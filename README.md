# Stripe Dispute Answer Automation

Auto-prepares a complete **evidence draft** on Stripe the moment a dispute is created.
A human reviews and submits in the dashboard — or Stripe's native auto-submit sends the
staged evidence at the deadline if no one acts. An LLM does the reasoning (works with
**any** provider); the code does the plumbing.

- **Bring your own model** — OpenAI, xAI (Grok), Anthropic, Groq, OpenRouter, DeepSeek, Mistral, or a local model.
- **Deploy anywhere** — one-command Vercel, or a Docker image that runs on any host.
- **Drafts, never blind submissions** — nothing is irreversibly submitted by the automation.
- **Stateless & private** — no customer data is stored; working files are transient.

> ⚠️ This tool *prepares* evidence. Review drafts before relying on auto-submit. It is not legal advice.

---

## How it works

```
Stripe charge.dispute.created ──► /webhook (Vercel function or your server)
        │  verifies the Stripe signature
        ▼
  gather facts (Stripe + optional product usage + optional support history + IP geo)
        │
        ▼  the LLM reasons over the case, picks the right Stripe evidence fields,
           writes the rebuttal + per-field text, sets a confidence level
        │
        ▼  render PDFs · attach policy / receipt / support-contact proof
        ▼  stage a DRAFT (submit=false) · notify (Slack/any webhook)
        ▼
  Review & submit in Stripe — or Stripe auto-submits at the deadline
```

The model adapts to **every** dispute reason (subscription canceled, duplicate, credit not
processed, product unacceptable, fraudulent, etc.), selecting the matching Stripe evidence
fields. Thin or contradictory cases are flagged `recommend_manual` with a note, instead of
submitting a weak argument.

---

## What this ships (and what you swap)

This is a **template**. Stripe is the only required integration — disputes live there.
Everything else is a default adapter you can keep, skip, or replace with your own stack.

| Role | Default in this repo | Swap it? |
|---|---|---|
| Payments / disputes | **Stripe** (required) | No — this tool stages evidence on Stripe. |
| Reasoning model | Any OpenAI-compatible Chat Completions API | Yes — env only (`LLM_*`). OpenAI, Grok, Anthropic, Groq, OpenRouter, a local model, … |
| Product usage | **PostHog** | Yes — leave unset to skip, or replace `_gather_posthog` in `dispute_brain.py` with Amplitude, Mixpanel, your DB, etc. You also pass **your** event names via `POSTHOG_USAGE_EVENTS`. |
| Support history | **Help Scout** | Yes — leave unset to skip, or replace `_gather_helpscout` / `_helpscout_pdf` with Zendesk, Intercom, Freshdesk, Gmail, … |
| Support mailbox on evidence | `SUPPORT_EMAIL` | Yes — your public support address. |
| Refund / cancellation policy | `assets/policy.pdf` (generic sample) | Yes — drop in your published policy, or set `POLICY_PDF_PATH`. |
| IP geolocation | ipinfo.io | Yes — replace `_gather_ip`, or skip (it fails open). |
| Notifications | Slack incoming webhook, or any URL | Yes — `NOTIFY_WEBHOOK_URL`. Slack gets a formatted message; any other URL gets JSON. |
| Host | Vercel function, or Docker / any Python host | Yes — see Deploy below. |

Skipping an optional adapter is the common path: unset its env vars and that gather step is a no-op. Swapping one means editing the matching function in `dispute_brain.py` (each is ~30 lines and returns a small dict / a count / a PDF). The rest of the pipeline does not care where the facts came from.

---

## 1. Configure the model (any provider)

Set three env vars. The client speaks the OpenAI Chat Completions protocol, which nearly
every provider exposes.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` (example — use your provider's current id) |
|---|---|---|
| OpenAI | *(leave empty)* | `gpt-4o` |
| xAI (Grok) | `https://api.x.ai/v1` | `grok-2-latest` |
| Anthropic | `https://api.anthropic.com/v1/` | your Claude model id |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | any listed model |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| Mistral | `https://api.mistral.ai/v1` | `mistral-large-latest` |
| Local (Ollama) | `http://localhost:11434/v1` | `llama3.1` (set `LLM_API_KEY` to any non-empty value) |

```
LLM_API_KEY=...        # your provider key
LLM_BASE_URL=...       # from the table (empty for OpenAI)
LLM_MODEL=...          # from the table
```

## 2. Set the rest of the env

Copy `.env.example` and fill it in (see that file for the full annotated list). Minimum:

```
STRIPE_KEY=             # restricted key: dispute_write + read on charges/customers/subscriptions/invoices/files
STRIPE_WEBHOOK_SECRET=  # from the Stripe webhook you create in step 4
LLM_API_KEY= LLM_BASE_URL= LLM_MODEL=
MERCHANT_NAME=          # your business name
MERCHANT_DESCRIPTION=   # one line on what you sell
MERCHANT_POLICY_SUMMARY=# one paragraph summarizing your refund/cancellation terms
SUPPORT_EMAIL=          # public support mailbox (printed on evidence)
```

Replace `assets/policy.pdf` with your own published refund/cancellation policy (or point
`POLICY_PDF_PATH` at it). The bundled file is a generic sample, not a real merchant policy.

### Optional: product usage (PostHog by default)

The bundled adapter talks to **PostHog**. To use it, set:

```
POSTHOG_KEY=
POSTHOG_PROJECT_ID=
POSTHOG_USAGE_EVENTS=signup,project_created,export_completed
```

`POSTHOG_USAGE_EVENTS` is **your** analytics event names, comma-separated. Pick the
events that show real product use (account created, a project saved, an export, a
login, etc.). This template does not ship any product-specific event names; if you
omit the list, PostHog still contributes geo/device, but usage counts stay empty.

Not on PostHog? Leave these unset (usage proof is skipped), or replace
`_gather_posthog` in `dispute_brain.py` with a call to Amplitude, Mixpanel, your
warehouse, etc. Return the same small shape: `geo`, `os`, `usage_events`.

### Optional: support-contact proof (Help Scout by default)

The bundled adapter talks to **Help Scout**. To use it, set:

```
HELPSCOUT_CLIENT_ID=
HELPSCOUT_CLIENT_SECRET=
```

Used to show the bank whether the cardholder contacted your support mailbox
(`SUPPORT_EMAIL`) before filing.

Not on Help Scout? Leave these unset (the support-contact PDF is skipped), or
replace `_gather_helpscout` and `_helpscout_pdf` in `dispute_brain.py` with
Zendesk, Intercom, Freshdesk, or whatever you search by customer email. The
brain only needs a conversation count (and, if you want, a PDF of the exchange).

### Optional: notifications

`NOTIFY_WEBHOOK_URL` — a Slack incoming webhook (auto-formatted) or any URL
(receives the summary as JSON). Leave unset to stay silent.

## 3. Deploy

**Vercel** (serverless, simplest):
```
vercel deploy --prod          # set the env vars in the Vercel project settings
```
Webhook URL: `https://<your-app>.vercel.app/api/stripe_webhook`

**Docker** (runs on Fly.io, Railway, Render, Cloud Run, a VPS — anywhere):
```
docker build -t dispute-bot .
docker run -p 8080:8080 --env-file .env dispute-bot
```
Webhook URL: `https://<your-host>/webhook`

**Plain Python** (a VPS, a `systemd` service):
```
pip install -r requirements.txt
python server.py              # listens on $PORT (default 8080), POST /webhook
```

> Other simple hosts people use: **Fly.io** / **Railway** / **Render** (point them at the Dockerfile),
> **Google Cloud Run** (`gcloud run deploy --source .`), or **AWS Lambda** (wrap `webhook_core.process`).

## 4. Create the Stripe webhook

In the Stripe Dashboard → Developers → Webhooks → add an endpoint to your deployed URL,
subscribe to **`charge.dispute.created`**, and copy its signing secret into
`STRIPE_WEBHOOK_SECRET`. Redeploy so the secret is loaded.

---

## Files

| File | Role |
|---|---|
| `dispute_brain.py` | Gather → LLM reasoning → stage draft → notify. Swap `_gather_posthog` / `_gather_helpscout` here. |
| `webhook_core.py` | Shared: verify Stripe signature + dispatch. |
| `api/stripe_webhook.py` | Vercel entry point (thin). |
| `server.py` | Standalone stdlib server (Docker / any host). |
| `Dockerfile`, `requirements.txt`, `pyproject.toml`, `vercel.json` | Build/deploy config. |
| `assets/policy.pdf` | Sample policy — replace with your own. |
| `.env.example` | Annotated env template. |

## Run one dispute manually (stages a reversible draft)

```
python -c "import dispute_brain; print(dispute_brain.handle_dispute('du_XXX'))"
```

## Security

- **Serverless / stateless** — no always-on host to attack, nothing stored to exfiltrate.
- **Restricted Stripe key** — cannot move money or read card numbers; lock it to your egress IP if you can.
- **Signature-verified** — unsigned requests are rejected with `400`; only Stripe can trigger the brain.
- **Secrets in the platform's env store only** — never committed. `.gitignore` blocks every `.env*` (except `.env.example`).
- **Idempotent** — a re-run skips a dispute that already has evidence staged, so Stripe retries are safe.

## License

MIT — see `LICENSE`.
