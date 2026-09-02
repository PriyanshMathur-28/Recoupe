"""Phase 9-10 stopping-rule and batch audit tests."""
from __future__ import annotations

import csv
import socket
import sqlite3

import pytest

from batch_runner import run_batch, run_event, summarize_results
from modules.messenger import GmailAuthError, GmailDeliveryError, _gmail_timeout_seconds, send_email
from modules.attempt_tracker import check_escalation, get_attempt_count
from modules.detector import get_all_risk_events


def test_third_consecutive_action_escalates_without_third_message(tmp_path):
    attempts = tmp_path / "attempts.sqlite3"
    audit = tmp_path / "audit.csv"
    event = {
        "event_type": "no_show",
        "client_id": "FAKE-3X",
        "client_name": "Fake Client",
        "client_email": "fake@example.com",
        "appointment_datetime": "2026-09-01T10:00:00+05:30",
        "appointment_value": 500,
        "urgency_hours": 1,
        "is_first_offense": False,
        "validation_errors": [],
        "source": "test",
    }
    messages = []

    def fake_llm(prompt):
        messages.append(prompt)
        return "client message"

    first = run_event(event, attempts, audit, llm_call=fake_llm)
    second = run_event(event, attempts, audit, llm_call=fake_llm)
    third = run_event(event, attempts, audit, llm_call=fake_llm)

    assert first["action"] == second["action"] == "charge_fee"
    assert third["action"] == "escalate_human"
    assert third["message"] is None
    assert third["client_notified"] is False
    assert len(messages) == 2
    assert get_attempt_count("FAKE-3X", attempts) == 3
    assert check_escalation("FAKE-3X", attempts) is True
    with sqlite3.connect(attempts) as connection:
        flag = connection.execute("SELECT client_id, reason FROM escalation_flags").fetchone()
    assert flag[0] == "FAKE-3X"
    assert "attempt 3" in flag[1]


def test_batch_processes_50_valid_rows_cleanly(tmp_path):
    attempts = tmp_path / "attempts.sqlite3"
    audit = tmp_path / "audit.csv"
    results = run_batch(
        reset_audit=True,
        attempts_path=attempts,
        audit_path=audit,
        event_loader=lambda: get_all_risk_events(include_calendar=False),
    )
    assert len(results) == 50
    with audit.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 50
    assert sum(row["status"] == "clean" for row in rows) == 50
    assert sum(row["status"] == "flagged_error" for row in rows) == 0


def test_batch_continues_after_payment_action_failure(tmp_path):
    attempts = tmp_path / "attempts.sqlite3"
    audit = tmp_path / "audit.csv"
    events = [
        {
            "event_type": "failed_subscription",
            "client_id": "BROKEN-PAYMENT",
            "client_name": "Broken Payment",
            "client_email": "broken@example.com",
            "subscription_amount": 250,
            "attempt_count": 0,
            "failure_reason": "card_declined",
            "validation_errors": [],
            "source": "test",
        },
        {
            "event_type": "failed_subscription",
            "client_id": "AFTER-FAILURE",
            "client_name": "After Failure",
            "client_email": "after@example.com",
            "subscription_amount": 300,
            "attempt_count": 0,
            "failure_reason": "card_declined",
            "validation_errors": [],
            "source": "test",
        },
    ]

    def failing_event(event, *args, **kwargs):
        if event["client_id"] == "BROKEN-PAYMENT":
            raise RuntimeError("payment-link creation failed")
        return run_event(event, *args, **kwargs)

    import batch_runner
    original = batch_runner.run_event
    batch_runner.run_event = failing_event
    try:
        results = run_batch(attempts_path=attempts, audit_path=audit, event_loader=lambda: events)
    finally:
        batch_runner.run_event = original

    assert len(results) == 2
    assert results[0]["action"] == "escalate_human"
    assert "payment-link creation failed" in results[0]["error"]
    assert results[1]["event"]["client_id"] == "AFTER-FAILURE"
    with audit.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["status"] == "flagged_error"
    assert rows[1]["status"] == "clean"


def test_batch_logs_csv_loader_failure(tmp_path):
    audit = tmp_path / "audit.csv"
    results = run_batch(audit_path=audit, event_loader=lambda: (_ for _ in ()).throw(ValueError("malformed CSV")))
    assert len(results) == 1
    assert results[0]["action"] == "escalate_human"
    assert "malformed CSV" in results[0]["error"]
    with audit.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["status"] == "flagged_error"
    assert "malformed CSV" in row["errors"]


def test_batch_continues_after_non_dict_event(tmp_path):
    audit = tmp_path / "audit.csv"
    results = run_batch(
        attempts_path=tmp_path / "attempts.sqlite3",
        audit_path=audit,
        event_loader=lambda: [None, {"event_type": "friendly_reminder", "client_id": "VALID", "client_name": "Valid", "source": "test"}],
    )
    assert len(results) == 2
    assert results[0]["action"] == "escalate_human"
    assert results[0]["audit"]["status"] == "flagged_error"
    assert results[1]["event"]["client_id"] == "VALID"


def test_batch_summary_distinguishes_preview_from_recovered_revenue():
    results = [
        {"event": {"action": "charge_fee", "appointment_value": 500}, "action": "charge_fee", "payment_status": "preview_created", "audit": {"status": "clean"}},
        {"event": {"subscription_amount": 750}, "action": "retry_payment", "payment_status": "recovered", "audit": {"status": "clean"}},
        {"event": {}, "action": "escalate_human", "payment_status": "not_applicable", "audit": {"status": "flagged_error"}},
    ]
    summary = summarize_results(results)
    assert summary["cases_processed"] == 3
    assert summary["payment_links_created"] == 2
    assert summary["revenue_at_risk"] == 1250
    assert summary["revenue_recovered"] == 750
    assert summary["escalations"] == 1
    assert summary["flagged_errors"] == 1


def test_gmail_timeout_requires_a_positive_number(monkeypatch):
    monkeypatch.setenv("GMAIL_HTTP_TIMEOUT_SECONDS", "12.5")
    assert _gmail_timeout_seconds() == 12.5

    for value in ("0", "-1", "not-a-number"):
        monkeypatch.setenv("GMAIL_HTTP_TIMEOUT_SECONDS", value)
        with pytest.raises(RuntimeError, match="must be a positive number"):
            _gmail_timeout_seconds()


def _gmail_raising(error: Exception):
    """A minimal Gmail service stub whose send request raises ``error``."""

    class Request:
        def execute(self):
            raise error

    class Messages:
        def send(self, **kwargs):
            return Request()

    class Users:
        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    return Gmail()


def test_gmail_delivery_errors_stay_runtime_errors():
    """Existing broad handlers must keep treating these as technical errors.

    ``run_event`` and the bulk-send loop already catch every failure and audit it
    as ``technical_error``; typing the Gmail boundary must not change which
    handler runs, only how precisely the failure can be described.
    """
    assert issubclass(GmailAuthError, GmailDeliveryError)
    assert issubclass(GmailDeliveryError, RuntimeError)


def test_a_revoked_gmail_token_is_a_typed_auth_error_naming_the_fix():
    """The refresh fires inside ``.execute()``, so it must be caught there.

    Untyped, this escaped every ``except`` clause on the dashboard's send route
    and surfaced as a bare HTTP 500 with no instruction for the operator.
    """
    exceptions = pytest.importorskip("google.auth.exceptions")
    service = _gmail_raising(exceptions.RefreshError("invalid_grant: Token has been expired or revoked."))

    with pytest.raises(GmailAuthError) as caught:
        send_email("client@example.com", "Payment retry", "Please retry.", service=service)

    message = str(caught.value)
    assert "invalid_grant" in message
    assert "oauth_flow.py" in message
    assert "No email was sent" in message


def test_a_gmail_transport_failure_is_reported_as_one_delivery_error():
    service = _gmail_raising(socket.timeout("Gmail request timed out"))

    with pytest.raises(GmailDeliveryError) as caught:
        send_email("client@example.com", "Payment retry", "Please retry.", service=service)

    assert "timed out" in str(caught.value)
    assert not isinstance(caught.value, GmailAuthError)


def test_an_unusable_recipient_is_still_a_plain_value_error():
    """Validation precedes the transport, so a bad address is not a Gmail fault."""
    with pytest.raises(ValueError, match="valid recipient email"):
        send_email("not-an-address", "Payment retry", "Please retry.", service=_gmail_raising(RuntimeError("unreachable")))


def test_live_batch_continues_after_gmail_timeout(tmp_path):
    class Request:
        def __init__(self, recipient):
            self.recipient = recipient

        def execute(self):
            if self.recipient == "timeout@example.com":
                raise socket.timeout("Gmail request timed out")
            return {"id": "gmail_after_timeout"}

    class Messages:
        def send(self, **kwargs):
            import base64
            from email import message_from_bytes

            message = message_from_bytes(base64.urlsafe_b64decode(kwargs["body"]["raw"]))
            return Request(message["to"])

    class Users:
        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    events = [
        {
            "event_type": "no_show",
            "client_id": "GMAIL-TIMEOUT",
            "client_name": "Timeout Client",
            "client_email": "timeout@example.com",
            "urgency_hours": 1,
            "is_first_offense": True,
            "validation_errors": [],
            "source": "test",
        },
        {
            "event_type": "no_show",
            "client_id": "GMAIL-AFTER",
            "client_name": "After Client",
            "client_email": "after@example.com",
            "urgency_hours": 1,
            "is_first_offense": True,
            "validation_errors": [],
            "source": "test",
        },
    ]
    results = run_batch(
        attempts_path=tmp_path / "attempts.sqlite3",
        audit_path=tmp_path / "audit.csv",
        event_loader=lambda: events,
        live=True,
        message_service=Gmail(),
        llm_call=lambda _: "Recovery message",
    )

    assert len(results) == 2
    assert results[0]["action"] == "escalate_human"
    assert results[0]["audit"]["outcome"] == "technical_error"
    assert "timed out" in results[0]["error"]
    assert results[1]["action"] == "friendly_reminder"
    assert results[1]["client_notified"] is True
    assert results[1]["audit"]["outcome"] == "action_completed"
