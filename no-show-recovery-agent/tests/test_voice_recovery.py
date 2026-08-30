"""Tests for the voice-recovery contract: the five cards, the two-step outcome
rule, and the last-action attribution rule.

Each test pins one promise the dashboard makes to the operator, so a regression
shows up as a failing invariant rather than a wrong number on a card.
"""
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules import vapi_client
from modules.audit_log import FIELDS as AUDIT_FIELDS
from modules.razorpay_webhooks import (
    RECOVERY_FIELDS,
    get_recovery_record,
    write_recovery_record,
)
from modules.voice_calls import (
    ANSWERED_OUTCOMES,
    OUTCOMES,
    VoiceOutcomeError,
    answered_from_ended_reason,
    attribute_recovery,
    classify_reply,
    close_call,
    latest_call_placed_at,
    latest_email_sent_at,
    open_call,
    resolve_call_outcome,
    start_of_current_cycle,
    voice_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def _write_audit(path, rows):
    """Write an audit CSV projection with only the columns each row needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in AUDIT_FIELDS})


def _backdate(voice_path, call_id, stamp):
    """Move one attempt outside the cycle window; ``placed_at`` is otherwise stamped by open_call."""
    with sqlite3.connect(voice_path) as connection:
        connection.execute("UPDATE call_log SET placed_at = ? WHERE id = ?", (stamp, int(call_id)))


def _promise_caller(date="2026-09-04"):
    def caller(_transcript):
        return json.dumps({"outcome": "promised_to_pay", "promise_date": date, "summary": "Client agreed to pay.", "confidence": 0.9})

    return caller


@pytest.fixture
def paths(tmp_path):
    """Isolated stores so no test reads the developer's real data directory."""
    return {
        "voice": tmp_path / "voice_calls.sqlite3",
        "audit": tmp_path / "logs" / "audit_log.csv",
        "recovery": tmp_path / "recovered.sqlite3",
        "attempts": tmp_path / "attempts.sqlite3",
    }


# ---------------------------------------------------------------------------
# The cycle window (Cards 2, 3, 4 share one boundary)
# ---------------------------------------------------------------------------


def test_cycle_start_is_the_oldest_audit_row(paths):
    oldest = _iso(-10)
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}, {"timestamp": oldest}, {"timestamp": _iso(-5)}])
    assert start_of_current_cycle(paths["audit"]) == oldest


def test_cycle_start_is_none_when_no_cycle_has_run(paths):
    assert start_of_current_cycle(paths["audit"]) is None


def test_promises_and_answer_rate_use_the_same_window_as_calls_placed(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])

    stale = open_call("C-OLD", mode="demo", path=paths["voice"])
    close_call(stale["id"], outcome="promised_to_pay", answered=True, promise_date="2026-01-01", path=paths["voice"])
    _backdate(paths["voice"], stale["id"], _iso(-72))

    current = open_call("C-NEW", mode="demo", path=paths["voice"])
    close_call(current["id"], outcome="promised_to_pay", answered=True, promise_date="2026-09-01", path=paths["voice"])

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    # The stale attempt is excluded from every cycle-scoped card, not just Card 3.
    assert metrics["calls_placed"] == 1
    assert metrics["promises_captured"] == 1
    assert metrics["calls_completed"] == 1


def test_in_flight_call_counts_as_placed_but_not_in_answer_rate(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    answered = open_call("C-1", mode="demo", path=paths["voice"])
    close_call(answered["id"], outcome="declined", answered=True, path=paths["voice"])
    open_call("C-2", mode="demo", path=paths["voice"])  # still ringing

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["calls_placed"] == 2
    assert metrics["calls_in_flight"] == 1
    assert metrics["calls_completed"] == 1
    assert metrics["answer_rate"] == 100.0


def test_answer_rate_counts_every_non_no_answer_outcome_as_reached(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    for outcome, answered in (("promised_to_pay", True), ("declined", True), ("escalated", True), ("no_answer", False)):
        call = open_call(f"C-{outcome}", mode="demo", path=paths["voice"])
        close_call(call["id"], outcome=outcome, answered=answered, path=paths["voice"])

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["calls_answered"] == 3
    assert metrics["answer_rate"] == 75.0
    assert metrics["answer_rate"] is not None


def test_answer_rate_is_none_rather_than_zero_with_no_completed_calls(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    open_call("C-1", mode="demo", path=paths["voice"])
    assert voice_metrics(paths["voice"], paths["audit"], paths["recovery"])["answer_rate"] is None


# ---------------------------------------------------------------------------
# The call_log contract
# ---------------------------------------------------------------------------


def test_answered_call_cannot_be_no_answer(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="no_answer", answered=True, path=paths["voice"])


def test_unanswered_call_cannot_carry_a_reply_outcome(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="promised_to_pay", answered=False, path=paths["voice"])


def test_outcome_outside_the_enum_is_refused(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="answered", answered=True, path=paths["voice"])


def test_a_call_can_only_be_closed_once(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    close_call(call["id"], outcome="declined", answered=True, path=paths["voice"])
    with pytest.raises(ValueError):
        close_call(call["id"], outcome="promised_to_pay", answered=True, path=paths["voice"])


def test_promise_date_is_dropped_for_non_promise_outcomes(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    closed = close_call(call["id"], outcome="declined", answered=True, promise_date="2026-09-01", path=paths["voice"])
    assert closed["promise_date"] is None


def test_call_log_has_no_primary_channel_column(paths):
    open_call("C-1", mode="demo", path=paths["voice"])
    with sqlite3.connect(paths["voice"]) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(call_log)")}
    assert "primary_channel" not in columns
    assert "primary_channel" not in RECOVERY_FIELDS


# ---------------------------------------------------------------------------
# Attribution: last action before payment
# ---------------------------------------------------------------------------


def test_attribution_awards_the_call_when_it_came_last(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-3), "client_id": "C-1", "outcome": "invoice_sent"}])
    call = open_call("C-1", mode="demo", path=paths["voice"])

    via, triggered_at = attribute_recovery("C-1", audit_path=paths["audit"], attempts_path=paths["attempts"], voice_path=paths["voice"])
    assert via == "call"
    assert triggered_at == call["placed_at"]


def test_attribution_awards_the_email_when_it_came_last(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    _backdate(paths["voice"], call["id"], _iso(-5))
    email_at = _iso(-1)
    _write_audit(paths["audit"], [{"timestamp": email_at, "client_id": "C-1", "outcome": "invoice_sent"}])

    via, triggered_at = attribute_recovery("C-1", audit_path=paths["audit"], attempts_path=paths["attempts"], voice_path=paths["voice"])
    assert via == "email"
    assert triggered_at == email_at


def test_attribution_is_none_when_neither_channel_acted(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1), "client_id": "OTHER", "outcome": "invoice_sent"}])
    assert attribute_recovery("C-1", audit_path=paths["audit"], attempts_path=paths["attempts"], voice_path=paths["voice"]) == (None, None)


def test_attribution_uses_the_newest_of_several_attempts(paths):
    first = open_call("C-1", mode="demo", path=paths["voice"])
    _backdate(paths["voice"], first["id"], _iso(-6))
    second = open_call("C-1", mode="demo", path=paths["voice"])
    assert latest_call_placed_at("C-1", paths["voice"]) == second["placed_at"]


def test_only_delivered_email_outcomes_count_as_an_email_send(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1), "client_id": "C-1", "outcome": "escalated_to_human"}])
    assert latest_email_sent_at("C-1", paths["audit"], paths["attempts"]) is None


def test_attribution_is_persisted_in_the_same_write_as_the_amount(paths):
    triggered_at = _iso(-2)
    assert write_recovery_record("C-1", 1500.0, "plink_1", "evt_1", "payment_link.paid", paths["recovery"], recovered_via="call", recovery_triggered_at=triggered_at)
    record = get_recovery_record("C-1", paths["recovery"])
    # A row that exists with an amount but no channel would make Card 1 undercount.
    assert record["amount_recovered"] == 1500.0
    assert record["recovered_via"] == "call"
    assert record["recovery_triggered_at"] == triggered_at
    assert record["recovered_at"]


def test_duplicate_recovery_event_is_ignored(paths):
    assert write_recovery_record("C-1", 1500.0, "plink_1", "evt_1", "payment_link.paid", paths["recovery"], recovered_via="call", recovery_triggered_at=_iso(-2))
    assert not write_recovery_record("C-1", 1500.0, "plink_1", "evt_1", "payment_link.paid", paths["recovery"], recovered_via="call", recovery_triggered_at=_iso(-2))


# ---------------------------------------------------------------------------
# Cards 1 and 5 — the recovery side
# ---------------------------------------------------------------------------


def test_voice_revenue_is_a_subset_of_total_recovered(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    write_recovery_record("C-CALL", 1000.0, None, "evt_1", "payment_link.paid", paths["recovery"], recovered_via="call", recovery_triggered_at=_iso(-2))
    write_recovery_record("C-MAIL", 400.0, None, "evt_2", "payment_link.paid", paths["recovery"], recovered_via="email", recovery_triggered_at=_iso(-2))

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["recovered_via_voice"] == 1000.0
    assert metrics["recovered_via_email"] == 400.0
    # Channels partition the total; they never stack on top of it.
    assert metrics["total_recovered"] == 1400.0
    assert metrics["recovered_via_voice"] + metrics["recovered_via_email"] == metrics["total_recovered"]


def test_avg_time_to_payment_is_none_until_the_first_voice_recovery(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    write_recovery_record("C-MAIL", 400.0, None, "evt_1", "payment_link.paid", paths["recovery"], recovered_via="email", recovery_triggered_at=_iso(-2))

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    # None is what renders as an em dash. Zero would claim instant payment.
    assert metrics["avg_hours_to_payment"] is None
    assert metrics["avg_sample_size"] == 0


def test_avg_time_to_payment_subtracts_triggered_at_without_a_join(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    # Several attempts on the same case: the old ambiguous join would have had to
    # pick one. The persisted triggering instant removes the choice.
    for _ in range(3):
        open_call("C-1", mode="demo", path=paths["voice"])
    write_recovery_record("C-1", 900.0, None, "evt_1", "payment_link.paid", paths["recovery"], recovered_via="call", recovery_triggered_at=_iso(-4))

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["avg_sample_size"] == 1
    assert metrics["avg_hours_to_payment"] == pytest.approx(4.0, abs=0.1)


def test_recovery_without_attribution_is_excluded_from_voice_revenue(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    write_recovery_record("C-1", 500.0, None, "evt_1", "payment_link.paid", paths["recovery"])
    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["recovered_via_voice"] == 0
    assert metrics["total_recovered"] == 500.0


# ---------------------------------------------------------------------------
# The two-step outcome rule
# ---------------------------------------------------------------------------


def test_step_one_silence_short_circuits_classification():
    def exploding_caller(_transcript):
        raise AssertionError("an unanswered call must never reach the classifier")

    resolved = resolve_call_outcome(answered=False, transcript="", caller=exploding_caller)
    assert resolved["outcome"] == "no_answer"
    assert resolved["answered"] is False


def test_step_two_classifies_an_answered_call_four_ways():
    resolved = resolve_call_outcome(answered=True, transcript="Yes I will pay tomorrow", caller=_promise_caller())
    assert resolved["outcome"] == "promised_to_pay"
    assert resolved["promise_date"] == "2026-09-04"
    assert resolved["answered"] is True


def test_classifier_never_returns_no_answer_even_on_garbage():
    def garbage(_transcript):
        return "not json at all"

    result = classify_reply("mmm hello who is this", garbage)
    assert result["outcome"] in ANSWERED_OUTCOMES
    assert result["outcome"] != "no_answer"


def test_classifier_never_invents_a_promise_from_an_unreadable_reply():
    def garbage(_transcript):
        return ""

    assert classify_reply("...", garbage)["outcome"] == "escalated"


def test_answered_is_not_a_fifth_outcome():
    assert "answered" not in OUTCOMES
    assert set(OUTCOMES) == {"promised_to_pay", "declined", "no_answer", "escalated"}


def test_provider_hangup_reasons_map_to_step_one():
    assert answered_from_ended_reason("customer-did-not-answer", "anything") is False
    assert answered_from_ended_reason("customer-ended-call", "I will pay") is True
    assert answered_from_ended_reason("customer-ended-call", "") is False


# ---------------------------------------------------------------------------
# Demo Mode closes by exactly the same rule as a web call
# ---------------------------------------------------------------------------


def test_demo_and_web_close_through_one_function():
    assert vapi_client.complete_demo_call is vapi_client.complete_web_call


def test_demo_silence_beyond_the_window_is_no_answer(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="",
        speech_detected=False,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        caller=_promise_caller(),
    )
    assert result["call"]["outcome"] == "no_answer"
    assert result["call"]["answered"] is False


def test_demo_speech_after_the_window_is_still_no_answer(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="Yes I will pay tomorrow",
        speech_detected=True,
        seconds_to_first_speech=vapi_client.SILENCE_WINDOW_SECONDS + 1,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        caller=_promise_caller(),
    )
    assert result["call"]["outcome"] == "no_answer"


def test_demo_speech_inside_the_window_runs_the_four_way_classifier(paths):
    call = open_call("C-1", mode="demo", path=paths["voice"])
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="Yes I will pay tomorrow",
        speech_detected=True,
        seconds_to_first_speech=1.5,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        caller=_promise_caller(),
    )
    assert result["call"]["outcome"] == "promised_to_pay"
    assert result["call"]["promise_date"] == "2026-09-04"


def test_the_second_closing_report_is_a_no_op_not_an_error(paths):
    """The browser and the webhook both report; whichever lands first wins."""
    call = open_call("C-1", mode="demo", path=paths["voice"])
    first = vapi_client.complete_web_call(call["id"], transcript="I will pay", speech_detected=True, seconds_to_first_speech=1.0, voice_path=paths["voice"], audit_path=paths["audit"], caller=_promise_caller())
    second = vapi_client.complete_web_call(call["id"], transcript="I will pay", speech_detected=True, seconds_to_first_speech=1.0, voice_path=paths["voice"], audit_path=paths["audit"], caller=_promise_caller())
    assert first["handled"] is True
    assert second["handled"] is False
    assert second["duplicate"] is True


def test_placing_a_call_records_the_attempt_before_the_browser_connects(paths, monkeypatch):
    monkeypatch.setenv("VOICE_DEMO_MODE", "true")
    result = vapi_client.start_web_call("C-1", client_name="Asha", amount=500, voice_path=paths["voice"], audit_path=paths["audit"])
    assert result["mode"] == "demo"
    assert result["web"] is None
    assert result["call"]["placed_at"]
    assert result["call"]["outcome"] is None


# ---------------------------------------------------------------------------
# Provider boundary
# ---------------------------------------------------------------------------


def test_webhook_refuses_everything_when_no_secret_is_configured(monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    headers = {"X-Vapi-Secret": "anything"}
    assert vapi_client.verify_webhook(b"{}", headers, secret="") is False


def test_webhook_accepts_the_configured_shared_secret():
    headers = {"X-Vapi-Secret": "s3cret"}
    assert vapi_client.verify_webhook(b"{}", headers, secret="s3cret") is True
    assert vapi_client.verify_webhook(b"{}", {"X-Vapi-Secret": "wrong"}, secret="s3cret") is False


def test_webhook_accepts_a_body_signature():
    import hashlib
    import hmac

    body = b'{"message":{"type":"end-of-call-report"}}'
    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert vapi_client.verify_webhook(body, {"X-Vapi-Signature": digest}, secret="s3cret") is True
    assert vapi_client.verify_webhook(body, {"X-Vapi-Signature": "00" * 32}, secret="s3cret") is False


def test_unverified_webhook_delivery_is_rejected_with_401(monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    body, status = vapi_client.ingest_webhook(b'{"message":{}}', {}, secret="")
    assert status == 401
    assert body["ok"] is False


def test_malformed_verified_payload_is_rejected_with_400():
    body, status = vapi_client.ingest_webhook(b"not json", {"X-Vapi-Secret": "s3cret"}, secret="s3cret")
    assert status == 400


def test_config_status_never_exposes_a_credential(monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk_visible")
    monkeypatch.setenv("VAPI_PRIVATE_KEY", "sk_must_not_leak")
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "whsec_must_not_leak")
    serialized = json.dumps(vapi_client.config_status())
    assert "sk_must_not_leak" not in serialized
    assert "whsec_must_not_leak" not in serialized


def test_web_call_payload_carries_only_the_public_key(paths, monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk_visible")
    monkeypatch.setenv("VAPI_PRIVATE_KEY", "sk_must_not_leak")
    monkeypatch.delenv("VOICE_DEMO_MODE", raising=False)
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    result = vapi_client.start_web_call("C-1", client_name="Asha", amount=500, voice_path=paths["voice"], audit_path=paths["audit"])
    serialized = json.dumps(result)
    assert result["web"]["public_key"] == "pk_visible"
    assert "sk_must_not_leak" not in serialized
    # The row id travels in metadata so the server-push report can find it.
    assert result["web"]["metadata"]["call_log_id"] == result["call"]["id"]


# ---------------------------------------------------------------------------
# HTTP boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    import dashboard

    dashboard.app.config.update(TESTING=True)
    return dashboard.app.test_client()


def test_vapi_webhook_route_is_registered(client, monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    response = client.post("/webhooks/vapi", data=b'{"message":{}}', content_type="application/json")
    # Registered and refusing an unsigned delivery — not a 404.
    assert response.status_code == 401


def test_starting_a_call_sends_no_email(client, monkeypatch):
    """Attribution compares call time against email time; an email sent in the
    same instant as the call would make that comparison meaningless."""
    import dashboard

    def fail_if_used():
        raise AssertionError("starting a call must not touch the email service")

    monkeypatch.setattr(dashboard, "_service", fail_if_used)
    monkeypatch.setattr(dashboard, "start_web_call", lambda *args, **kwargs: {"call": {"id": 1}, "mode": "demo", "web": None})
    response = client.post("/api/voice/start-call", json={"case_id": "C-1"})
    assert response.status_code == 200
    assert response.get_json()["call"]["id"] == 1


def test_starting_a_call_requires_a_case_id(client):
    assert client.post("/api/voice/start-call", json={}).status_code == 400


def test_completing_a_call_requires_a_call_id(client):
    assert client.post("/api/voice/complete-call", json={}).status_code == 400
