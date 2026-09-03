"""Regression tests for limitations A-H and run-state isolation."""
from __future__ import annotations

import csv

from batch_runner import run_batch, run_event
from dashboard import calculate_metrics
from main import process_event
from modules.attempt_tracker import get_attempt_count
from modules.detector import get_all_risk_events
from modules.waitlist import add_to_waitlist


class FakePaymentLink:
    def create(self, payload):
        return {"id": "plink_fix", "short_url": "https://pay.test/fix"}


class FakePaymentClient:
    def __init__(self):
        self.payment_link = FakePaymentLink()


class FakeRequest:
    def execute(self):
        return {"id": "gmail_fix"}


class FakeMessages:
    def __init__(self):
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeRequest()


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


def subscription(client_id="SUB-FIX", attempt_count=0, **overrides):
    return {
        "event_type": "failed_subscription",
        "client_id": client_id,
        "client_name": "Subscriber",
        "client_email": "subscriber@example.com",
        "subscription_amount": 500,
        "attempt_count": attempt_count,
        "validation_errors": [],
        "source": "test",
        **overrides,
    }


def test_live_action_really_sends_message(tmp_path):
    gmail = FakeGmail()
    result = run_event(subscription(), tmp_path / "attempts.sqlite3", tmp_path / "audit.csv", payment_client=FakePaymentClient(), llm_call=lambda _: "Retry now", live=True, message_service=gmail)
    assert result["client_notified"] is True
    assert len(gmail.resource.resource.sent) == 1


def test_waitlist_database_drives_detection_and_live_fifo_notification(tmp_path, monkeypatch):
    waitlist = tmp_path / "waitlist.sqlite3"
    add_to_waitlist({"client_id": "WAIT-1", "client_name": "First", "client_email": "first@example.com"}, waitlist)
    monkeypatch.setattr("modules.detector.check_no_shows", lambda: [{"event_type": "no_show", "client_id": "NS-1", "client_name": "Cancelled", "appointment_datetime": "2026-09-01", "urgency_hours": 8, "is_first_offense": False, "validation_errors": [], "source": "test"}])
    monkeypatch.setattr("modules.detector.check_failed_subscriptions", lambda: [])
    event = get_all_risk_events(include_calendar=False, waitlist_path=waitlist)[0]
    assert event["waitlist_entry_exists"] is True
    result = run_event(event, tmp_path / "attempts.sqlite3", tmp_path / "audit.csv", live=True, message_service=FakeGmail(), waitlist_path=waitlist, llm_call=lambda _: "Slot available")
    assert result["action"] == "offer_waitlist"
    assert result["client_notified"] is True


def test_preview_enforces_live_payment_prerequisites(tmp_path):
    result = run_event(subscription(subscription_amount=None), tmp_path / "attempts.sqlite3", tmp_path / "audit.csv")
    assert result["action"] == "escalate_human"
    assert "positive amount" in result["error"]


def test_only_payment_actions_consume_stopping_budget(tmp_path):
    attempts = tmp_path / "attempts.sqlite3"
    audit = tmp_path / "audit.csv"
    reminder = {"event_type": "no_show", "client_id": "SHARED", "client_name": "Asha", "client_email": "a@example.com", "is_first_offense": True, "urgency_hours": 1, "validation_errors": [], "source": "test"}
    run_event(reminder, attempts, audit)
    run_event(reminder, attempts, audit)
    payment = subscription(client_id="SHARED")
    result = run_event(payment, attempts, audit)
    assert result["action"] == "retry_payment"
    assert get_attempt_count("SHARED", attempts) == 1


def test_technical_payment_failure_does_not_consume_attempt(tmp_path):
    attempts = tmp_path / "attempts.sqlite3"
    result = run_event(subscription(), attempts, tmp_path / "audit.csv", live=True, payment_client=object())
    assert result["action"] == "escalate_human"
    assert result["audit"]["outcome"] == "technical_error"
    assert get_attempt_count("SUB-FIX", attempts) == 0


def test_subscription_source_and_agent_attempts_are_reconciled(tmp_path):
    result = run_event(subscription(attempt_count=2), tmp_path / "attempts.sqlite3", tmp_path / "audit.csv")
    assert result["action"] == "escalate_human"
    assert result["attempt_count"] == 3


def test_changed_event_payload_reopens_scheduler_record(tmp_path):
    store = tmp_path / "state.sqlite3"
    kwargs = {"store_path": store, "audit_path": tmp_path / "audit.csv", "attempts_path": tmp_path / "attempts.sqlite3"}
    first = process_event(subscription(), **kwargs)
    changed = process_event(subscription(failure_reason="expired_card"), **kwargs)
    duplicate = process_event(subscription(failure_reason="expired_card"), **kwargs)
    assert first.get("skipped") is not True
    assert changed.get("skipped") is not True
    assert duplicate["skipped"] is True


def test_missing_identifiers_do_not_collapse_distinct_invalid_rows(monkeypatch, tmp_path):
    malformed = [
        {"event_type": "no_show", "client_id": None, "client_name": "One", "validation_errors": ["missing client_id"], "source": "test"},
        {"event_type": "no_show", "client_id": None, "client_name": "Two", "validation_errors": ["missing client_id"], "source": "test"},
    ]
    monkeypatch.setattr("modules.detector.check_no_shows", lambda: malformed)
    monkeypatch.setattr("modules.detector.check_failed_subscriptions", lambda: [])
    assert len(get_all_risk_events(False, tmp_path / "waitlist.sqlite3")) == 2


def test_audit_outcome_and_dashboard_success_exclude_human_review(tmp_path):
    audit = tmp_path / "audit.csv"
    run_batch(attempts_path=tmp_path / "attempts.sqlite3", audit_path=audit, event_loader=lambda: [subscription(), {"event_type": "unknown", "client_id": "X", "validation_errors": [], "source": "test"}])
    with audit.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["outcome"] for row in rows] == ["action_completed", "human_review"]
    assert calculate_metrics(rows)["success_actions"] == 1
