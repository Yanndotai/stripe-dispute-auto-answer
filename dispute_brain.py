"""
Dispute brain — prepares Stripe dispute evidence as a DRAFT (submit=false).

The REASONING is done by an LLM: it adapts to ANY dispute reason, picks the right
Stripe evidence fields, and writes the rebuttal statement + per-field text. The Python
here does only deterministic I/O: gather data (Stripe + optional PostHog + optional
Help Scout + IP geo), render PDFs, upload, stage the draft, notify. Stripe's native
auto-submit sends the staged evidence at the deadline if no human reviewed earlier.

Provider-agnostic: any OpenAI-compatible Chat Completions endpoint works — OpenAI,
xAI (Grok), Groq, OpenRouter, DeepSeek, Mistral, a local model (Ollama/LM Studio),
or Anthropic's OpenAI-compatible endpoint. Configure with LLM_API_KEY / LLM_BASE_URL /
LLM_MODEL.

Stateless: nothing persisted, temp files in /tmp disappear after the request.

Env vars:
  STRIPE_KEY                  restricted key (dispute_write + read charges/customers/...)
  LLM_API_KEY                 the reasoning engine's API key (any provider)
  LLM_BASE_URL                OpenAI-compatible base URL (omit for OpenAI itself)
  LLM_MODEL                   model id (e.g. gpt-4o, grok-2-latest, your Claude model id)
  MERCHANT_NAME               your business name (shown to the model + on evidence)
  MERCHANT_DESCRIPTION        one line on what you sell
  MERCHANT_POLICY_SUMMARY     one-paragraph summary of your refund/cancellation terms
  SUPPORT_EMAIL               your public support mailbox (shown on evidence)
  POLICY_PDF_PATH             path to your refund/cancellation policy PDF (attached as evidence)
  POSTHOG_KEY / POSTHOG_PROJECT_ID   optional product-usage enrichment
  POSTHOG_USAGE_EVENTS        comma-separated event names that prove the customer used your product
  HELPSCOUT_CLIENT_ID / HELPSCOUT_CLIENT_SECRET  optional support-contact proof
  NOTIFY_WEBHOOK_URL          optional Slack incoming webhook (or any URL — gets JSON)
"""
import os
import json
import textwrap
import tempfile
from datetime import datetime, timezone
from collections import Counter

import requests
import stripe
from openai import OpenAI
from fpdf import FPDF

stripe.api_key = os.environ["STRIPE_KEY"]

POSTHOG_KEY = os.environ.get("POSTHOG_KEY")
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "")
POSTHOG_USAGE_EVENTS = {
    e.strip() for e in os.environ.get("POSTHOG_USAGE_EVENTS", "").split(",") if e.strip()
}
HELPSCOUT_CLIENT_ID = os.environ.get("HELPSCOUT_CLIENT_ID")
HELPSCOUT_CLIENT_SECRET = os.environ.get("HELPSCOUT_CLIENT_SECRET")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")

MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "the merchant")
MERCHANT_DESCRIPTION = os.environ.get("MERCHANT_DESCRIPTION", "a digital product / SaaS sold online")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
MERCHANT_POLICY_SUMMARY = os.environ.get(
    "MERCHANT_POLICY_SUMMARY",
    "The merchant's published refund & cancellation policy is presented to and accepted by "
    "the customer at checkout before purchase.",
)

ASSETS = os.path.join(os.path.dirname(__file__), "assets")
POLICY_PDF = os.environ.get("POLICY_PDF_PATH", os.path.join(ASSETS, "policy.pdf"))

# Stripe evidence TEXT fields the model may fill (validated before we send to Stripe).
ALLOWED_TEXT_FIELDS = {
    "uncategorized_text", "cancellation_rebuttal", "cancellation_policy_disclosure",
    "refund_policy_disclosure", "refund_refusal_explanation", "duplicate_charge_explanation",
    "product_description", "customer_name", "customer_email_address", "billing_address",
    "customer_purchase_ip", "service_date", "duplicate_charge_id",
}
_FIELDS_FOR_TEXT = sorted(ALLOWED_TEXT_FIELDS - {"uncategorized_text"})


def _resolve_llm():
    """Resolve provider config. Convenience: a bare ANTHROPIC_API_KEY/OPENAI_API_KEY works."""
    key = os.environ.get("LLM_API_KEY")
    base = os.environ.get("LLM_BASE_URL") or None
    model = os.environ.get("LLM_MODEL")
    if not key and os.environ.get("ANTHROPIC_API_KEY"):
        key = os.environ["ANTHROPIC_API_KEY"]
        base = base or "https://api.anthropic.com/v1/"
        model = model or "claude-opus-4-8"
    if not key and os.environ.get("OPENAI_API_KEY"):
        key = os.environ["OPENAI_API_KEY"]
    if not key:
        raise RuntimeError("No LLM API key set (LLM_API_KEY).")
    return key, base, (model or "gpt-4o")


def _system_prompt():
    return f"""You are a Stripe dispute-defense analyst for {MERCHANT_NAME} ({MERCHANT_DESCRIPTION}). \
You receive a structured case file (customer, charges, subscription, invoices, product usage, \
IP geolocation, support contact history) for ONE dispute, and you produce the evidence to stage \
on Stripe.

Your job: pick the right Stripe evidence fields for the dispute's reason, write a concise, formal \
rebuttal statement addressed to the issuing bank reviewer, and write any per-field text. Lead with \
the strongest evidence. Be factual and grounded ONLY in the case file — never invent dates, usage, \
IPs, or communications. English only (banks review in English).

The merchant's refund/cancellation policy: {MERCHANT_POLICY_SUMMARY}
The merchant's public support mailbox: {SUPPORT_EMAIL or "(not configured)"}

PER-CATEGORY FIELD GUIDANCE (Stripe evidence object):

FRAUDULENT / UNRECOGNIZED — prove the legitimate holder authorized & used the service. Use
  uncategorized_text (usage/login/IP timeline) and customer_communication. Cite IP-to-billing
  match, prior non-disputed charges, product usage, locale.
CREDIT_NOT_PROCESSED — why no refund is owed. Use refund_refusal_explanation,
  refund_policy_disclosure, and attach the refund/cancellation policy. Cite the no-refund terms
  and that no refund request came through support.
DUPLICATE — prove the charge is distinct. Use duplicate_charge_explanation (distinct billing
  period / invoice) and uncategorized_text. Set duplicate_charge_id ONLY to a real prior
  non-disputed charge id from the case file if one clearly is the alleged duplicate.
GENERAL — partial-use/cancellation refund claim. Use refund_refusal_explanation,
  refund_policy_disclosure, attach policy. Cite usage + no-refund terms.
PRODUCT_NOT_RECEIVED — prove access/delivery. Use uncategorized_text (usage timeline, login)
  and customer_communication.
PRODUCT_UNACCEPTABLE — product matched description and worked. Use product_description,
  uncategorized_text (usage proving it functioned), customer_communication.
SUBSCRIPTION_CANCELED — prove the charge predates / is independent of any cancellation and that
  recurring terms were accepted. Use cancellation_rebuttal, cancellation_policy_disclosure,
  attach the cancellation policy. Cite charge date vs cancellation date, paid history, usage.
NONCOMPLIANT — written compliance explanation in uncategorized_text.

ALWAYS, for every category:
- Put a full activity/timeline log (account, billing history, usage, IP) in uncategorized_text.
- Write the main rebuttal as statement_text (it becomes a PDF on uncategorized_file).
- Attach the support (non-)contact proof when available (attach_customer_communication).
- For subscription_canceled / credit_not_processed / general (refund-or-cancellation reasons),
  attach the refund & cancellation policy (attach_policy=true) — decisive evidence.
- Attach the receipt when one is available (attach_receipt).

CONFIDENCE: set confidence="high" and recommend_manual=false only when the case file gives a
clear, factual, winnable argument. If the facts are thin, contradictory, or the reason needs
evidence not present in the case file, set confidence="low" and recommend_manual=true and explain
why in notes — a human will strengthen it before the deadline. Never fabricate to reach high."""


USER_TEMPLATE = """Here is the dispute case file (all facts gathered from Stripe and optional \
enrichment sources). Produce the evidence to stage.

```json
{case_json}
```

Return ONLY a JSON object (no markdown, no prose) with EXACTLY these keys:
- "reason_understanding": string
- "statement_text": string — the main rebuttal, formal English, lead with the strongest evidence
- "uncategorized_text": string — a full activity/timeline log (account, billing, usage, IP)
- "text_fields": array of objects {{"field": <one of {fields}>, "value": <string>}} — do NOT include uncategorized_text here
- "attach_policy": boolean
- "attach_receipt": boolean
- "attach_customer_communication": boolean
- "confidence": "high" or "low"
- "recommend_manual": boolean
- "notes": string — a short note for the human reviewer"""


# --------------------------------------------------------------------------- utils
def _plain(obj):
    if isinstance(obj, stripe.ListObject):
        return [_plain(x) for x in obj.data]
    if isinstance(obj, stripe.StripeObject):
        return obj._to_dict_recursive()
    return obj


def _ts(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if epoch else None


def _date(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d") if epoch else None


def _statement_to_pdf(text):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=9)
    for line in (text or "").split("\n"):
        line = line.encode("latin-1", "replace").decode("latin-1")
        if not line:
            pdf.ln(4)
            continue
        for chunk in (textwrap.wrap(line, width=95) or [""]):
            pdf.cell(0, 4, chunk, new_x="LMARGIN", new_y="NEXT")
    fd, path = tempfile.mkstemp(prefix="evidence-statement-", suffix=".pdf", dir="/tmp")
    os.close(fd)
    pdf.output(path)
    return path


def _upload(path):
    with open(path, "rb") as fh:
        return stripe.File.create(purpose="dispute_evidence", file=fh).id


def _parse_json(text):
    """Tolerant JSON extraction: strip code fences, take the outermost object."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


def _coerce(plan):
    """Apply defaults so a slightly-off model response never crashes the pipeline."""
    return {
        "reason_understanding": str(plan.get("reason_understanding", "")),
        "statement_text": str(plan.get("statement_text", "")).strip(),
        "uncategorized_text": str(plan.get("uncategorized_text", "")).strip(),
        "text_fields": [tf for tf in (plan.get("text_fields") or [])
                        if isinstance(tf, dict) and tf.get("field") and tf.get("value")],
        "attach_policy": bool(plan.get("attach_policy", False)),
        "attach_receipt": bool(plan.get("attach_receipt", False)),
        "attach_customer_communication": bool(plan.get("attach_customer_communication", False)),
        "confidence": plan.get("confidence", "low"),
        "recommend_manual": bool(plan.get("recommend_manual", True)),
        "notes": str(plan.get("notes", "")),
    }


# ----------------------------------------------------------------- data gathering
def _gather(dispute, charge, customer):
    cus_id = customer["id"]
    charges = _plain(stripe.Charge.list(customer=cus_id, limit=100))
    subs = _plain(stripe.Subscription.list(customer=cus_id, status="all", limit=10))
    invoices = _plain(stripe.Invoice.list(customer=cus_id, limit=100))

    sub = subs[0] if subs else None
    period = (None, None)
    if charge.get("invoice"):
        try:
            inv = _plain(stripe.Invoice.retrieve(charge["invoice"]))
            p = inv["lines"]["data"][0].get("period", {})
            period = (p.get("start"), p.get("end"))
        except Exception:
            pass

    addr = customer.get("address") or {}
    billing = ", ".join(x for x in [addr.get("line1"), addr.get("line2"), addr.get("city"),
                                    addr.get("state"), addr.get("postal_code"), addr.get("country")] if x)

    def charge_row(c):
        oc = c.get("outcome") or {}
        return {"id": c["id"], "date": _date(c["created"]), "amount": c["amount"] / 100,
                "currency": c["currency"].upper(), "disputed": c["disputed"], "refunded": c["refunded"],
                "risk_level": oc.get("risk_level"), "risk_score": oc.get("risk_score"),
                "type": (c.get("payment_method_details") or {}).get("type")}

    def inv_row(i):
        return {"id": i["id"], "number": i.get("number"), "amount": i["amount_paid"] / 100,
                "status": i.get("status"), "date": _date(i["created"]), "charge": i.get("charge")}

    return {
        "customer": {"id": cus_id, "name": customer.get("name"), "email": customer.get("email"),
                     "created": _ts(customer.get("created")), "billing_address": billing,
                     "locales": customer.get("preferred_locales")},
        "subscription": ({"id": sub["id"], "status": sub["status"],
                          "amount": (sub["items"]["data"][0]["price"]["unit_amount"] or 0) / 100,
                          "interval": sub["items"]["data"][0]["price"]["recurring"]["interval"],
                          "created": _ts(sub.get("created")), "canceled_at": _ts(sub.get("canceled_at")),
                          "cancel_at_period_end": sub.get("cancel_at_period_end")} if sub else None),
        "disputed_charge": charge_row(charge) | {
            "service_period": [_date(period[0]), _date(period[1])] if period[0] else None},
        "all_charges": [charge_row(c) for c in charges],
        "invoices": [inv_row(i) for i in invoices],
    }


def _gather_posthog(email):
    if not (POSTHOG_KEY and POSTHOG_PROJECT_ID):
        return {}
    try:
        base = f"https://app.posthog.com/api/projects/{POSTHOG_PROJECT_ID}"
        h = {"Authorization": f"Bearer {POSTHOG_KEY}"}
        results = requests.get(f"{base}/persons/?search={email}", headers=h, timeout=20).json().get("results", [])
        if not results:
            return {}
        person = results[0]
        props = person.get("properties", {})
        did = (person.get("distinct_ids") or [None])[0]
        usage = []
        if did and POSTHOG_USAGE_EVENTS:
            ev = requests.get(f"{base}/events/?distinct_id={did}&limit=300", headers=h, timeout=25).json().get("results", [])
            usage = [f"{v}x {k}" for k, v in Counter(
                e["event"] for e in ev if e["event"] in POSTHOG_USAGE_EVENTS
            ).most_common()]
        return {"geo": f'{props.get("$geoip_city_name")}, {props.get("$geoip_country_name")}',
                "os": props.get("$os"), "usage_events": usage}
    except Exception:
        return {}


def _gather_helpscout(email):
    if not (HELPSCOUT_CLIENT_ID and HELPSCOUT_CLIENT_SECRET):
        return None
    try:
        tok = requests.post("https://api.helpscout.net/v2/oauth2/token",
                            data={"grant_type": "client_credentials", "client_id": HELPSCOUT_CLIENT_ID,
                                  "client_secret": HELPSCOUT_CLIENT_SECRET}, timeout=20).json()["access_token"]
        r = requests.get(f'https://api.helpscout.net/v2/conversations?query=(email:"{email}")&status=all',
                         headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        return r.json().get("page", {}).get("totalElements", 0)
    except Exception:
        return None


def _gather_ip(ip):
    if not ip:
        return {}
    try:
        return requests.get(f"https://ipinfo.io/{ip}/json", timeout=15).json()
    except Exception:
        return {}


def _helpscout_pdf(email, count):
    mailbox = SUPPORT_EMAIL or "(not configured)"
    body = ["Customer Communication Record", f"Merchant: {MERCHANT_NAME}", "",
            f"Customer email searched : {email}",
            f"Support mailbox          : {mailbox}",
            "Search scope             : all conversations, all statuses, all dates",
            f"Conversations found      : {count}", ""]
    if count == 0:
        body.append("A full-history search of the merchant's support system for the cardholder's email "
                     "returns 0 conversations. The cardholder never contacted support to request a "
                     "cancellation, report a problem, or ask for a refund before filing this dispute.")
    else:
        body.append(f"The support system shows {count} conversation(s) with the cardholder; see the "
                    "merchant's support records for the full exchange.")
    return _statement_to_pdf("\n".join(body))


# ----------------------------------------------------------------- the brain (LLM)
def _reason(case):
    key, base_url, model = _resolve_llm()
    client = OpenAI(api_key=key, base_url=base_url)
    messages = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": USER_TEMPLATE.format(
                    case_json=json.dumps(case, indent=2), fields=_FIELDS_FOR_TEXT)}]
    resp = None
    for extra in ({"response_format": {"type": "json_object"}}, {}):  # graceful fallback
        try:
            resp = client.chat.completions.create(model=model, messages=messages, **extra)
            break
        except Exception:
            continue
    if resp is None:
        resp = client.chat.completions.create(model=model, messages=messages)
    return _coerce(_parse_json(resp.choices[0].message.content))


# ------------------------------------------------------------------------- main
def handle_dispute(dispute_id):
    """Idempotent: stage a DRAFT (submit=false). Returns a summary dict."""
    dispute = _plain(stripe.Dispute.retrieve(dispute_id))
    ed = dispute.get("evidence_details", {})
    if dispute["status"] != "needs_response":
        return {"skipped": f"status={dispute['status']}", "dispute": dispute_id}
    if ed.get("submission_count", 0) > 0:
        return {"skipped": "already submitted", "dispute": dispute_id}
    if ed.get("has_evidence"):
        return {"skipped": "evidence already staged", "dispute": dispute_id}

    charge = _plain(stripe.Charge.retrieve(dispute["charge"]))
    customer = _plain(stripe.Customer.retrieve(charge["customer"]))
    email = customer.get("email") or ""
    purchase_ip = (dispute.get("evidence") or {}).get("customer_purchase_ip")

    case = _gather(dispute, charge, customer)
    case["dispute"] = {"id": dispute_id, "reason": dispute["reason"],
                       "amount": dispute["amount"] / 100, "currency": dispute["currency"].upper(),
                       "due_by": _ts(ed.get("due_by"))}
    case["posthog"] = _gather_posthog(email)
    case["ip_geolocation"] = _gather_ip(purchase_ip)
    hs_count = _gather_helpscout(email)
    case["helpscout_conversations"] = hs_count
    case["support_email"] = SUPPORT_EMAIL or None
    case["merchant_policy"] = MERCHANT_POLICY_SUMMARY

    plan = _reason(case)

    evidence = {}
    evidence["uncategorized_file"] = _upload(_statement_to_pdf(plan["statement_text"]))
    if plan["uncategorized_text"]:
        evidence["uncategorized_text"] = plan["uncategorized_text"]
    for tf in plan["text_fields"]:
        if tf["field"] in ALLOWED_TEXT_FIELDS:
            evidence[tf["field"]] = tf["value"]

    if plan["attach_policy"] and os.path.exists(POLICY_PDF):
        evidence["cancellation_policy"] = _upload(POLICY_PDF)
    if plan["attach_customer_communication"] and hs_count is not None:
        evidence["customer_communication"] = _upload(_helpscout_pdf(email, hs_count))
    if plan["attach_receipt"]:
        try:
            rurl = charge.get("receipt_url")
            if rurl:
                pdf_url = rurl.replace("?s=ap", "/pdf?s=ap") if "?s=ap" in rurl else rurl + "/pdf"
                r = requests.get(pdf_url, timeout=20)
                if r.ok and r.content[:4] == b"%PDF":
                    fd, p = tempfile.mkstemp(suffix=".pdf", dir="/tmp")
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(r.content)
                    evidence["receipt"] = _upload(p)
        except Exception:
            pass

    stripe.Dispute.modify(dispute_id, evidence=evidence, submit=False)

    summary = {"dispute": dispute_id, "customer": email, "reason": dispute["reason"],
               "amount": f"{dispute['amount']/100:.2f} {dispute['currency'].upper()}",
               "due_by": _ts(ed.get("due_by")), "fields_staged": list(evidence.keys()),
               "helpscout_conversations": hs_count, "confidence": plan["confidence"],
               "recommend_manual": plan["recommend_manual"], "notes": plan["notes"],
               "status": "draft_ready"}
    _notify(summary)
    return summary


def _notify(summary):
    if not NOTIFY_WEBHOOK_URL:
        return
    try:
        payload = _slack_message(summary) if "hooks.slack.com" in NOTIFY_WEBHOOK_URL else summary
        requests.post(NOTIFY_WEBHOOK_URL, json=payload, timeout=15)
    except Exception:
        pass


def _slack_message(s):
    dash = f"https://dashboard.stripe.com/disputes/{s['dispute']}"
    flag = "  :warning: *needs manual review*" if s.get("recommend_manual") else "  :white_check_mark: *high confidence*"
    hs = s.get("helpscout_conversations")
    hs_line = (f"Support: {hs} prior conversation(s)" if hs else "Support: no prior contact") if hs is not None else ""
    note = f"\n_Note:_ {s['notes']}" if s.get("notes") else ""
    text = (":shield: *Dispute evidence draft ready*\n"
            f"*{s.get('customer','')}* · *{s.get('amount','')}* · `{s.get('reason','')}`\n"
            f"Deadline: {s.get('due_by','?')}\n"
            f"Fields staged: {', '.join(s.get('fields_staged', []))}\n"
            f"{hs_line}{flag}{note}\n"
            f"<{dash}|Review & submit in Stripe →>  ·  _Stripe auto-submits at the deadline if untouched._")
    return {"text": text}
