"""Regression coverage for audited edge cases and Phase 7-8 modules."""
import base64
from email import message_from_bytes

import pytest

from modules.decision_engine import decide
from modules.detector import normalize_event
from modules.handlers import handle_action
from modules.message_generator import generate_message
from modules.messenger import send_message
from modules.payments import PaymentLinkProviderError, create_payment_link
from modules.waitlist import add_to_waitlist, get_next_in_line, mark_slot


class FakePaymentLink:
    def __init__(self, response=None):
        self.response = response or {"id": "plink_1", "short_url": "https://pay.test/1"}
        self.payload = None

    def create(self, payload):
        self.payload = payload
        return self.response


class FakePaymentClient:
    def __init__(self, response=None):
        self.payment_link = FakePaymentLink(response)


class FailingPaymentLink:
    def create(self, payload):
        from razorpay.errors import ServerError

        raise ServerError("test mode limit of 30 reached for payment_link")


class FailingPaymentClient:
    def __init__(self):
        self.payment_link = FailingPaymentLink()


class FakeSendRequest:
    def execute(self):
        return {"id": "gmail_1"}


class FakeMessages:
    def __init__(self):
        self.body = None

    def send(self, **kwargs):
        self.body = kwargs["body"]
        return FakeSendRequest()


class FakeUsers:
    def __init__(self):
        self.resource = FakeMessages()

    def messages(self):
        return self.resource


class FakeGmail:
    def __init__(self):
        self.resource = FakeUsers()

    def users(self):
        return self.resource


def test_decision_rejects_negative_and_fractional_values():
    assert decide({"event_type": "no_show", "urgency_hours": -5}) == "escalate_human"
    assert decide({"event_type": "failed_subscription", "attempt_count": -1}) == "escalate_human"
    assert decide({"event_type": "failed_subscription", "attempt_count": 2.9}) == "escalate_human"


def test_detector_rejects_unknown_sources_and_invalid_identifiers():
    with pytest.raises(ValueError, match="Unsupported event source"):
        normalize_event("mystery", {})
    event = normalize_event("subscription", {"client_id": None, "attempt_count": -1, "subscription_amount": 500, "client_email": "a@example.com"})
    assert event["client_id"] is None
    assert "missing client_id" in event["validation_errors"]
    assert "invalid attempt_count" in event["validation_errors"]


def test_handler_blocks_invalid_events_and_missing_contacts():
    with pytest.raises(ValueError, match="validation errors"):
        handle_action({"validation_errors": ["bad amount"]}, "retry_payment")
    with pytest.raises(ValueError, match="phone or email"):
        handle_action({"subscription_amount": 500}, "retry_payment", payment_client=FakePaymentClient(), llm_call=lambda _: "message")


def test_payment_validation_is_consistent_and_requires_complete_response():
    for value in ("bad", None, float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValueError):
            create_payment_link(value, "Asha", "Fee", "a@example.com", client=FakePaymentClient())
    with pytest.raises(ValueError, match="zero paise"):
        create_payment_link("0.001", "Asha", "Fee", "a@example.com", client=FakePaymentClient())
    with pytest.raises(RuntimeError, match="incomplete"):
        create_payment_link(10, "Asha", "Fee", "a@example.com", client=FakePaymentClient({"id": "plink_1"}))


def test_payment_link_test_mode_limit_has_an_actionable_error():
    with pytest.raises(PaymentLinkProviderError, match="Test Mode has reached its payment-link limit"):
        create_payment_link(10, "Asha", "Fee", "a@example.com", client=FailingPaymentClient())


def test_falsey_callable_is_used_for_message_generation():
    class FalseyCallable:
        def __bool__(self):
            return False

        def __call__(self, prompt):
            return "injected"

    assert generate_message({"client_name": "Asha"}, "friendly_reminder", llm=FalseyCallable()) == "injected"


def test_waitlist_preserves_fifo_order(tmp_path):
    database = tmp_path / "waitlist.sqlite3"
    first = add_to_waitlist({"client_id": "C1", "client_name": "Asha", "client_email": "asha@example.com"}, database)
    add_to_waitlist({"client_id": "C2", "client_name": "Ravi", "client_email": "ravi@example.com"}, database)
    assert get_next_in_line(database)["id"] == first["id"]
    assert mark_slot("open", database) == "open"
    assert mark_slot("filled", database) == "filled"


def test_messenger_appends_payment_link_and_encodes_email():
    service = FakeGmail()
    result = send_message("owner@example.com", "Payment retry", "Please retry.", "https://pay.test/1", service=service)
    assert result["id"] == "gmail_1"
    raw = base64.urlsafe_b64decode(service.resource.resource.body["raw"])
    message = message_from_bytes(raw)
    assert message["to"] == "owner@example.com"
    assert "Payment link: https://pay.test/1" in message.get_payload(decode=True).decode("utf-8")


def test_payment_action_attaches_invoice_pdf():
    service = FakeGmail()
    result = handle_action(
        {"client_id": "SUB001", "client_name": "Asha", "client_email": "owner@example.com", "subscription_amount": 500, "attempt_count": 0},
        "retry_payment", payment_client=FakePaymentClient(), message_service=service, llm_call=lambda _: "Please retry.", deliver=True,
    )
    assert result["invoice_number"].startswith("INV-")
    raw = base64.urlsafe_b64decode(service.resource.resource.body["raw"])
    message = message_from_bytes(raw)
    attachments = [part for part in message.walk() if part.get_filename()]
    assert attachments and attachments[0].get_filename() == result["invoice_filename"]
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True).startswith(b"%PDF-")
