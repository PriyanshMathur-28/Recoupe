"""Tests for message generation and Razorpay payment-link integration."""
import csv
import hashlib
import hmac
import json

import pytest

from modules.handlers import handle_action
from modules.message_generator import TEMPLATES, generate_message
from modules.payments import create_payment_link
from modules.razorpay_webhooks import ingest_webhook


class FakePaymentLink:
    def __init__(self):
        self.payload = None

    def create(self, payload):
        self.payload = payload
        return {"id": "plink_test_123", "short_url": "https://rzp.io/i/test123"}


class FakeRazorpayClient:
    def __init__(self):
        self.payment_link = FakePaymentLink()


def capture_prompt(prompt):
    return prompt


@pytest.mark.parametrize("action", ["charge_fee", "offer_waitlist", "friendly_reminder", "retry_payment"])
def test_generate_message_uses_each_action_template(action):
    event = {"client_name": "Asha", "appointment_datetime": "2026-09-01 10:00", "appointment_value": 500, "failure_reason": "card_expired", "short_url": "https://rzp.io/i/test123"}
    prompt = generate_message(event, action, llm=capture_prompt)
    assert "Asha" in prompt
    assert TEMPLATES[action].split(".")[0] in prompt


def test_payment_link_converts_rupees_to_paise():
    client = FakeRazorpayClient()
    response = create_payment_link(499.50, "Asha", "Late cancellation fee", "+919999999999", client=client)
    assert response["id"] == "plink_test_123"
    assert client.payment_link.payload["amount"] == 49950
    assert client.payment_link.payload["currency"] == "INR"


@pytest.mark.parametrize("action,amount_field", [("charge_fee", "appointment_value"), ("retry_payment", "subscription_amount")])
def test_payment_actions_store_link_metadata(action, amount_field):
    event = {"client_name": "Asha", "client_phone": "+919999999999", amount_field: 500}
    result = handle_action(event, action, payment_client=FakeRazorpayClient(), llm_call=lambda prompt: "Natural message")
    assert result["payment_link_id"] == "plink_test_123"
    assert result["short_url"] == "https://rzp.io/i/test123"
    assert result["message"] == "Natural message"
    assert "payment_link_id" not in event


def test_nonpayment_action_does_not_create_payment_link():
    result = handle_action({"client_name": "Asha"}, "friendly_reminder", payment_client=FakeRazorpayClient(), llm_call=lambda prompt: "Friendly message")
    assert "payment_link_id" not in result
    assert result["message"] == "Friendly message"


def test_invalid_payment_amount_is_rejected():
    with pytest.raises(ValueError):
        create_payment_link(0, "Asha", "Fee", "+919999999999", client=FakeRazorpayClient())


def test_paid_webhook_closes_audit_loop_once(tmp_path):
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_paid_1",
                    "amount": 50000,
                    "amount_paid": 50000,
                    "customer": {"name": "Asha", "email": "asha@example.com"},
                    "notes": {"client_id": "CLIENT-1", "recovery_action": "charge_fee"},
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":"))
    secret = "webhook-secret"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    webhook_db = tmp_path / "webhooks.sqlite3"
    audit = tmp_path / "audit.csv"

    first = ingest_webhook(body, signature, secret, "evt_1", webhook_db, audit)
    second = ingest_webhook(body, signature, secret, "evt_1", webhook_db, audit)

    assert first["duplicate"] is False
    assert first["event"]["payment_status"] == "recovered"
    assert first["event"]["appointment_value"] == 500.0
    assert first["audit"]["payment_status"] == "recovered"
    assert second == {"duplicate": True, "event_id": "evt_1", "event": first["event"]}
    with audit.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["action"] == "charge_fee"
    assert rows[0]["payment_status"] == "recovered"


def test_partial_payment_webhook_preserves_distinct_amounts(tmp_path):
    payload = {
        "event": "payment_link.partially_paid",
        "payload": {"payment_link": {"entity": {
            "id": "plink_partial_1", "amount": 100000, "amount_paid": 25000,
            "customer": {"name": "Asha", "email": "asha@example.com"},
            "notes": {"client_id": "CLIENT-1", "recovery_action": "retry_payment"},
        }}}
    }
    body = json.dumps(payload, separators=(",", ":"))
    secret = "webhook-secret"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    result = ingest_webhook(body, signature, secret, "evt_partial", tmp_path / "webhooks.sqlite3", tmp_path / "audit.csv")
    event = result["event"]
    assert event["payment_status"] == "partially_paid"
    assert event["amount_paid"] == 250.0
    assert event["amount_due"] == 750.0
    assert event["subscription_amount"] == 1000.0


def test_webhook_rejects_invalid_signature_before_parsing(tmp_path):
    with pytest.raises(ValueError, match="Invalid Razorpay webhook signature"):
        ingest_webhook("not-json", "wrong", "secret", "evt_bad", tmp_path / "webhooks.sqlite3", tmp_path / "audit.csv")
