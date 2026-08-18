"""
Shared webhook handling — used by both the Vercel function and the standalone server.

Verifies the Stripe signature, and on `charge.dispute.created` (live mode) runs the
brain to stage a DRAFT. Idempotent and safe under Stripe retries.
"""
import os
import stripe
import dispute_brain

WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")


def process(payload: bytes, sig_header: str):
    """Return (status_code, body_dict). Pure function — no framework coupling."""
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        return 400, {"error": f"signature verification failed: {e}"}

    if event["type"] != "charge.dispute.created":
        return 200, {"ignored": event["type"]}

    # Dashboard "Send test event" carries a fake dispute id + livemode=false.
    if not event.get("livemode"):
        return 200, {"test_event_ok": True, "type": event["type"]}

    dispute_id = event["data"]["object"]["id"]
    try:
        return 200, dispute_brain.handle_dispute(dispute_id)
    except Exception as e:
        # 500 → Stripe retries later; the idempotency guard prevents double-staging.
        return 500, {"error": str(e), "dispute": dispute_id}
