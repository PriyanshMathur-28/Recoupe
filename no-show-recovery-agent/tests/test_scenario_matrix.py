"""Scenario matrix coverage for no-shows, empty cancellation slots, and recurring payments."""
from __future__ import annotations

import csv
import sqlite3

import pytest

from batch_runner import run_batch, run_event
from modules.decision_engine import decide
from modules.detector import check_calendar_live, normalize_event
from modules.handlers import handle_action
from modules.waitlist import add_to_waitlist, get_next_in_line, mark_slot, notify_waitlist_person


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeCalendarEvents:
    def __init__(self, response):
        self.response = response

    def list(self, **kwargs):
        return FakeRequest(self.response)


class FakeCalendarService:
    def __init__(self, response):
        self.events_resource = FakeCalendarEvents(response)

    def events(self):
        return self.events_resource


class FakePaymentLink:
    def create(self, payload):
        return {"id": "plink_matrix", "short_url": "https://pay.test/matrix"}


class FakePaymentClient:
    def __init__(self):
        self.payment_link = FakePaymentLink()


class FakeSendRequest:
    def execute(self):
        return {"id": "gmail_matrix"}


class FakeMessages:
    def send(self, **kwargs):
        self.body = kwargs["body"]
        return FakeSendRequest()


class FakeUsers:
    def __init__(self):
        self.messages_resource = FakeMessages()

    def messages(self):
        return self.messages_resource


class FakeGmail:
    def __init__(self):
        self.users_resource = FakeUsers()

    def users(self):
        return self.users_resource


NO_SHOW_BASE = {
    "event_type": "no_show",
    "client_id": "NS-MATRIX",
    "client_name": "Matrix Client",
    "client_email": "matrix@example.com",
    "appointment_datetime": "2026-09-01T10:00:00+05:30",
    "appointment_value": 500,
    "validation_errors": [],
    "source": "test",
}


@pytest.mark.parametrize(
    "event_overrides, expected",
    [
        ({"is_first_offense": True, "urgency_hours": 0}, "friendly_reminder"),
        ({"is_first_offense": "yes", "urgency_hours": 1.99}, "friendly_reminder"),
        ({"is_first_offense": False, "urgency_hours": 0}, "charge_fee"),
        ({"is_first_offense": False, "urgency_hours": 1.999}, "charge_fee"),
        ({"is_first_offense": False, "urgency_hours": 2}, "escalate_human"),
        ({"is_first_offense": False, "urgency_hours": 24, "waitlist_entry_exists": True}, "offer_waitlist"),
        ({"is_first_offense": False, "urgency_hours": 24, "waitlist_entry_exists": False}, "escalate_human"),
        ({"is_first_offense": False, "urgency_hours": -1, "waitlist_entry_exists": True}, "escalate_human"),
        ({"is_first_offense": False, "urgency_hours": "invalid", "waitlist_entry_exists": True}, "escalate_human"),
    ],
)
def test_no_show_policy_matrix(event_overrides, expected):
    assert decide({**NO_SHOW_BASE, **event_overrides}) == expected


@pytest.mark.parametrize("invalid_value", [None, "", "NaN", float("nan"), "not-a-date"])
def test_no_show_normalization_marks_missing_client_or_datetime(invalid_value):
    event = normalize_event(
        "no_show",
        {
            **NO_SHOW_BASE,
            "client_id": invalid_value,
            "appointment_datetime": "2026-09-01T10:00:00+05:30",
            "cancellation_time": "2026-09-01T09:00:00+05:30",
        },
    )
    if invalid_value is None or invalid_value == "":
        assert "missing client_id" in event["validation_errors"]
    assert event["event_type"] == "no_show"


def test_no_show_batch_escalates_after_three_recovery_attempts(tmp_path):
    event = {**NO_SHOW_BASE, "urgency_hours": 1, "is_first_offense": False}
    results = [
        run_event(event, tmp_path / "attempts.sqlite3", tmp_path / "audit.csv")
        for _ in range(4)
    ]
    assert [result["action"] for result in results] == [
        "charge_fee",
        "charge_fee",
        "escalate_human",
        "escalate_human",
    ]
    assert all(result["client_notified"] is False for result in results[2:])


@pytest.mark.parametrize(
    "status, expected",
    [("cancelled", True), ("confirmed", False), ("tentative", False)],
)
def test_calendar_cancellation_detection_only_processes_cancelled_events(status, expected):
    service = FakeCalendarService(
        {
            "items": [
                {
                    "id": "CAL-MATRIX",
                    "summary": "Cancelled slot",
                    "status": status,
                    "start": {"dateTime": "2026-09-01T10:00:00+00:00"},
                    "updated": "2026-09-01T09:00:00+00:00",
                }
            ]
        }
    )
    events = check_calendar_live(service, now="2026-09-01T09:30:00+00:00")
    assert bool(events) is expected
    if expected:
        assert events[0]["event_type"] == "calendar_cancellation"
        assert events[0]["client_id"] == "CAL-MATRIX"


def test_cancelled_slot_without_waitlist_escalates_instead_of_fabricating_recipient():
    event = {
        "event_type": "calendar_cancellation",
        "client_id": "CAL-EMPTY",
        "client_name": "Cancelled Customer",
        "urgency_hours": 4,
        "waitlist_entry_exists": False,
        "is_first_offense": False,
        "validation_errors": [],
        "source": "google_calendar",
    }
    assert decide(event) == "escalate_human"


def test_cancelled_slot_with_waitlist_offers_slot_and_preserves_fifo(tmp_path):
    database = tmp_path / "waitlist.sqlite3"
    first = add_to_waitlist(
        {"client_id": "WAIT-1", "client_name": "First", "client_email": "first@example.com"},
        database,
    )
    add_to_waitlist(
        {"client_id": "WAIT-2", "client_name": "Second", "client_email": "second@example.com"},
        database,
    )
    assert get_next_in_line(database)["id"] == first["id"]
    result = notify_waitlist_person(
        {"appointment_datetime": "2026-09-01T10:00:00+00:00", "short_url": "https://calendar.test/slot"},
        database,
        service=FakeGmail(),
        llm=lambda prompt: "Slot available",
    )
    assert result["person"]["client_id"] == "WAIT-1"
    assert get_next_in_line(database)["client_id"] == "WAIT-2"


def test_cancelled_slot_with_empty_waitlist_is_explicitly_unactionable(tmp_path):
    database = tmp_path / "waitlist.sqlite3"
    with pytest.raises(LookupError, match="No waiting client"):
        notify_waitlist_person({"appointment_datetime": "2026-09-01T10:00:00+00:00"}, database)


@pytest.mark.parametrize("slot_status", ["open", "filled"])
def test_slot_status_supports_empty_slot_lifecycle(slot_status, tmp_path):
    assert mark_slot(slot_status, tmp_path / "waitlist.sqlite3") == slot_status


def test_slot_status_rejects_unknown_lifecycle_state(tmp_path):
    with pytest.raises(ValueError, match="open.*filled"):
        mark_slot("cancelled", tmp_path / "waitlist.sqlite3")


@pytest.mark.parametrize(
    "attempt_count, expected",
    [(0, "retry_payment"), (1, "retry_payment"), (2, "retry_payment"), (3, "escalate_human"), (4, "escalate_human")],
)
def test_recurring_membership_payment_attempt_policy_matrix(attempt_count, expected):
    assert decide({"event_type": "failed_subscription", "attempt_count": attempt_count}) == expected


@pytest.mark.parametrize("attempt_count", [None, "", "bad", -1, 1.5, True, float("nan"), float("inf")])
def test_recurring_membership_payment_invalid_attempts_escalate(attempt_count):
    assert decide({"event_type": "failed_subscription", "attempt_count": attempt_count}) == "escalate_human"


@pytest.mark.parametrize("field, value", [("subscription_amount", 0), ("subscription_amount", -20), ("client_email", "invalid")])
def test_recurring_payment_normalization_rejects_invalid_required_fields(field, value):
    row = {
        "client_id": "SUB-MATRIX",
        "client_name": "Subscriber",
        "client_email": "subscriber@example.com",
        "subscription_amount": 500,
        "attempt_count": 0,
    }
    row[field] = value
    event = normalize_event("subscription", row)
    assert event["validation_errors"]
    assert event["event_type"] == "failed_subscription"


def test_recurring_payment_retry_creates_link_with_subscription_amount():
    event = {
        "event_type": "failed_subscription",
        "client_id": "SUB-LINK",
        "client_name": "Subscriber",
        "client_email": "subscriber@example.com",
        "subscription_amount": 799,
        "attempt_count": 1,
        "failure_reason": "card_declined",
        "validation_errors": [],
    }
    result = handle_action(
        event,
        "retry_payment",
        payment_client=FakePaymentClient(),
        llm_call=lambda prompt: "Retry message",
    )
    assert result["payment_link_id"] == "plink_matrix"
    assert result["short_url"] == "https://pay.test/matrix"
    assert result["message"] == "Retry message"
    assert "payment_link_id" not in event


def test_recurring_payment_batch_escalates_at_limit_without_client_message(tmp_path):
    event = {
        "event_type": "failed_subscription",
        "client_id": "SUB-LIMIT",
        "client_name": "Subscriber",
        "client_email": "subscriber@example.com",
        "subscription_amount": 799,
        "attempt_count": 3,
        "failure_reason": "card_declined",
        "validation_errors": [],
        "source": "test",
    }
    result = run_event(event, tmp_path / "attempts.sqlite3", tmp_path / "audit.csv")
    assert result["action"] == "escalate_human"
    assert result["message"] is None
    assert result["client_notified"] is False
    assert result["audit"]["payment_status"] == "not_applicable"


def test_mixed_scenario_batch_processes_all_three_event_types(tmp_path):
    events = [
        {**NO_SHOW_BASE, "client_id": "MIX-NO-SHOW", "urgency_hours": 1, "is_first_offense": False},
        {
            "event_type": "calendar_cancellation",
            "client_id": "MIX-CANCEL",
            "client_name": "Cancelled",
            "urgency_hours": 8,
            "waitlist_entry_exists": False,
            "is_first_offense": False,
            "validation_errors": [],
            "source": "google_calendar",
        },
        {
            "event_type": "failed_subscription",
            "client_id": "MIX-SUB",
            "client_name": "Subscriber",
            "client_email": "subscriber@example.com",
            "subscription_amount": 450,
            "attempt_count": 0,
            "validation_errors": [],
            "source": "test",
        },
    ]
    audit = tmp_path / "audit.csv"
    results = run_batch(
        attempts_path=tmp_path / "attempts.sqlite3",
        audit_path=audit,
        event_loader=lambda: events,
    )
    assert [result["action"] for result in results] == ["charge_fee", "escalate_human", "retry_payment"]
    with audit.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["event_type"] for row in rows] == ["no_show", "calendar_cancellation", "failed_subscription"]
    assert all(row["status"] == "clean" for row in rows)


def test_batch_records_invalid_subscription_as_review_case(tmp_path):
    event = normalize_event(
        "subscription",
        {"client_id": "SUB-BAD", "client_name": "Bad Subscriber", "client_email": "bad", "subscription_amount": 0, "attempt_count": "x"},
    )
    result = run_batch(
        attempts_path=tmp_path / "attempts.sqlite3",
        audit_path=tmp_path / "audit.csv",
        event_loader=lambda: [event],
    )[0]
    assert result["action"] == "escalate_human"
    assert result["client_notified"] is False
    with sqlite3.connect(tmp_path / "attempts.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM client_attempts").fetchone()[0] == 0
