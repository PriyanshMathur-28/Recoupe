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
    FINAL_ANSWER_KINDS,
    OUTCOMES,
    VOICE_LINK_ACTION,
    VOICE_LINK_OUTCOME,
    VoiceOutcomeError,
    accepted_plan_offer,
    agent_only_transcript,
    answered_from_ended_reason,
    attribute_recovery,
    call_history,
    classify_reply,
    close_call,
    decide_follow_up_email,
    detect_plan_request,
    extract_final_answer,
    follow_up_email_for_call,
    get_call,
    heuristic_final_answer,
    heuristic_plan_request,
    latest_call_placed_at,
    latest_email_sent_at,
    open_call,
    resolve_call_outcome,
    start_of_current_cycle,
    validate_email_decision,
    validate_final_answer,
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


def _declined_caller(summary="Client cannot pay the full amount right now."):
    """A stand-in for the outcome model on a call that captured no promise."""

    def caller(_transcript):
        return json.dumps({"outcome": "declined", "summary": summary, "confidence": 0.9})

    return caller


def _email_caller(send=True):
    """A stand-in for the email-decision model, so no test reaches a provider."""

    def caller(_briefing):
        return json.dumps({"send_link": send, "reason": "Stubbed verdict.", "confidence": 0.9})

    return caller


def _never_called(_argument):
    raise AssertionError("the model must not be consulted on this path")


class _PaymentLink:
    """Stands in for Razorpay's payment_link resource."""

    def __init__(self):
        self.payload = None

    def create(self, payload):
        self.payload = payload
        return {"id": "plink_voice", "short_url": "https://pay.test/voice"}


class _PaymentClient:
    def __init__(self):
        self.payment_link = _PaymentLink()


class _Messages:
    def __init__(self):
        self.body = None

    def send(self, **kwargs):
        self.body = kwargs["body"]
        return self

    def execute(self):
        return {"id": "gmail_voice"}


class _Users:
    def __init__(self):
        self.resource = _Messages()

    def messages(self):
        return self.resource


class _Gmail:
    """Stands in for the Gmail service, so a send is observable but never real."""

    def __init__(self):
        self.resource = _Users()

    def users(self):
        return self.resource


@pytest.fixture
def paths(tmp_path):
    """Isolated stores so no test reads the developer's real data directory."""
    return {
        "voice": tmp_path / "voice_calls.sqlite3",
        "audit": tmp_path / "logs" / "audit_log.csv",
        "recovery": tmp_path / "recovered.sqlite3",
        "attempts": tmp_path / "attempts.sqlite3",
        "plans": tmp_path / "flexible_plans.sqlite3",
    }


@pytest.fixture(autouse=True)
def no_live_providers(monkeypatch):
    """Guarantee the whole file runs offline.

    Closing a call now asks a third typed question — the client's final answer —
    and not every test stubs that caller. With a key present in the developer's
    environment those tests would quietly place a real network call. Clearing the
    keys forces ``_call_llm`` to raise, which is the exact condition the
    deterministic heuristic fallback exists to handle.
    """
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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

    stale = open_call("C-OLD", mode="web", path=paths["voice"])
    close_call(stale["id"], outcome="promised_to_pay", answered=True, promise_date="2026-01-01", path=paths["voice"])
    _backdate(paths["voice"], stale["id"], _iso(-72))

    current = open_call("C-NEW", mode="web", path=paths["voice"])
    close_call(current["id"], outcome="promised_to_pay", answered=True, promise_date="2026-09-01", path=paths["voice"])

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    # The stale attempt is excluded from every cycle-scoped card, not just Card 3.
    assert metrics["calls_placed"] == 1
    assert metrics["promises_captured"] == 1
    assert metrics["calls_completed"] == 1


def test_in_flight_call_counts_as_placed_but_not_in_answer_rate(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    answered = open_call("C-1", mode="web", path=paths["voice"])
    close_call(answered["id"], outcome="declined", answered=True, path=paths["voice"])
    open_call("C-2", mode="web", path=paths["voice"])  # still ringing

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["calls_placed"] == 2
    assert metrics["calls_in_flight"] == 1
    assert metrics["calls_completed"] == 1
    assert metrics["answer_rate"] == 100.0


def test_answer_rate_counts_every_non_no_answer_outcome_as_reached(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    for outcome, answered in (("promised_to_pay", True), ("declined", True), ("escalated", True), ("no_answer", False)):
        call = open_call(f"C-{outcome}", mode="web", path=paths["voice"])
        close_call(call["id"], outcome=outcome, answered=answered, path=paths["voice"])

    metrics = voice_metrics(paths["voice"], paths["audit"], paths["recovery"])
    assert metrics["calls_answered"] == 3
    assert metrics["answer_rate"] == 75.0
    assert metrics["answer_rate"] is not None


def test_answer_rate_is_none_rather_than_zero_with_no_completed_calls(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    open_call("C-1", mode="web", path=paths["voice"])
    assert voice_metrics(paths["voice"], paths["audit"], paths["recovery"])["answer_rate"] is None


# ---------------------------------------------------------------------------
# The call_log contract
# ---------------------------------------------------------------------------


def test_answered_call_cannot_be_no_answer(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="no_answer", answered=True, path=paths["voice"])


def test_unanswered_call_cannot_carry_a_reply_outcome(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="promised_to_pay", answered=False, path=paths["voice"])


def test_outcome_outside_the_enum_is_refused(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="answered", answered=True, path=paths["voice"])


def test_a_call_can_only_be_closed_once(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    close_call(call["id"], outcome="declined", answered=True, path=paths["voice"])
    with pytest.raises(ValueError):
        close_call(call["id"], outcome="promised_to_pay", answered=True, path=paths["voice"])


def test_promise_date_is_dropped_for_non_promise_outcomes(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    closed = close_call(call["id"], outcome="declined", answered=True, promise_date="2026-09-01", path=paths["voice"])
    assert closed["promise_date"] is None


def test_a_call_can_only_be_placed_in_a_real_mode(paths):
    """There is no simulated mode left to open a row in."""
    with pytest.raises(ValueError):
        open_call("C-1", mode="demo", path=paths["voice"])


def test_call_log_has_no_primary_channel_column(paths):
    open_call("C-1", mode="web", path=paths["voice"])
    with sqlite3.connect(paths["voice"]) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(call_log)")}
    assert "primary_channel" not in columns
    assert "primary_channel" not in RECOVERY_FIELDS


# ---------------------------------------------------------------------------
# Attribution: last action before payment
# ---------------------------------------------------------------------------


def test_attribution_awards_the_call_when_it_came_last(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-3), "client_id": "C-1", "outcome": "invoice_sent"}])
    call = open_call("C-1", mode="web", path=paths["voice"])

    via, triggered_at = attribute_recovery("C-1", audit_path=paths["audit"], attempts_path=paths["attempts"], voice_path=paths["voice"])
    assert via == "call"
    assert triggered_at == call["placed_at"]


def test_attribution_awards_the_email_when_it_came_last(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
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
    first = open_call("C-1", mode="web", path=paths["voice"])
    _backdate(paths["voice"], first["id"], _iso(-6))
    second = open_call("C-1", mode="web", path=paths["voice"])
    assert latest_call_placed_at("C-1", paths["voice"]) == second["placed_at"]


def test_only_delivered_email_outcomes_count_as_an_email_send(paths):
    _write_audit(paths["audit"], [{"timestamp": _iso(-1), "client_id": "C-1", "outcome": "escalated_to_human"}])
    assert latest_email_sent_at("C-1", paths["audit"], paths["attempts"]) is None


def test_a_link_the_call_itself_caused_does_not_steal_the_recovery(paths):
    """The strongest attribution invariant. A promise captured on a call is
    followed by an email carrying the link — if that email registered as an
    email send it would win every comparison on time alone, and Card 1 would
    read zero forever."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    _write_audit(
        paths["audit"],
        [{"timestamp": _iso(1), "client_id": "C-1", "action": VOICE_LINK_ACTION, "outcome": VOICE_LINK_OUTCOME}],
    )

    assert latest_email_sent_at("C-1", paths["audit"], paths["attempts"]) is None
    via, triggered_at = attribute_recovery("C-1", audit_path=paths["audit"], attempts_path=paths["attempts"], voice_path=paths["voice"])
    assert via == "call"
    assert triggered_at == call["placed_at"]


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
        open_call("C-1", mode="web", path=paths["voice"])
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
# The browser call closes by the same rule the webhook does
# ---------------------------------------------------------------------------


def _close(paths, call_id, **kwargs):
    """Close a web call with the email gate switched off.

    Every test in this section is about the outcome, not the email, and
    ``auto_email=False`` is the one argument that guarantees no test can reach a
    live model or a live payment provider while asserting it.
    """
    return vapi_client.complete_web_call(
        call_id,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        caller=_promise_caller(),
        auto_email=False,
        **kwargs,
    )


def test_silence_beyond_the_window_is_no_answer(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(paths, call["id"], transcript="", speech_detected=False)
    assert result["call"]["outcome"] == "no_answer"
    assert result["call"]["answered"] is False


def test_a_late_first_word_cannot_erase_a_conversation(paths):
    """The silence window is powerless against a real transcript.

    A slow connection, a long assistant greeting or a hesitant client can all push
    the first client word past the window. Filing that as "nobody picked up" is
    what previously threw away a full twenty-turn conversation, so evidence of
    speech outranks the clock and the reply still reaches the classifier.
    """
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(
        paths,
        call["id"],
        transcript="Yes I will pay tomorrow",
        speech_detected=True,
        seconds_to_first_speech=vapi_client.SILENCE_WINDOW_SECONDS + 1,
    )
    assert result["call"]["answered"] is True
    assert result["call"]["outcome"] == "promised_to_pay"


def test_a_late_signal_without_a_transcript_is_still_no_answer(paths):
    """Timing only decides when there is nothing said to decide from."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(
        paths,
        call["id"],
        transcript="",
        speech_detected=True,
        seconds_to_first_speech=vapi_client.SILENCE_WINDOW_SECONDS + 1,
    )
    assert result["call"]["answered"] is False
    assert result["call"]["outcome"] == "no_answer"


def test_speech_inside_the_window_runs_the_four_way_classifier(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(paths, call["id"], transcript="Yes I will pay tomorrow", speech_detected=True, seconds_to_first_speech=1.5)
    assert result["call"]["outcome"] == "promised_to_pay"
    assert result["call"]["promise_date"] == "2026-09-04"


def test_the_second_closing_report_is_a_no_op_not_an_error(paths):
    """The browser and the webhook both report; whichever lands first wins."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    first = _close(paths, call["id"], transcript="I will pay", speech_detected=True, seconds_to_first_speech=1.0)
    second = _close(paths, call["id"], transcript="I will pay", speech_detected=True, seconds_to_first_speech=1.0)
    assert first["handled"] is True
    assert second["handled"] is False
    assert second["duplicate"] is True


def test_placing_a_call_requires_a_configured_public_key(paths, monkeypatch):
    """An unconfigured deployment gets a refusal, never a simulated attempt."""
    monkeypatch.delenv("VAPI_PUBLIC_KEY", raising=False)
    with pytest.raises(vapi_client.VapiConfigError):
        vapi_client.start_web_call("C-1", client_name="Asha", amount=500, voice_path=paths["voice"], audit_path=paths["audit"])
    assert latest_call_placed_at("C-1", paths["voice"]) is None


def test_placing_a_call_records_the_attempt_before_the_browser_connects(paths, monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk_visible")
    result = vapi_client.start_web_call("C-1", client_name="Asha", amount=500, voice_path=paths["voice"], audit_path=paths["audit"])
    assert result["mode"] == "web"
    assert result["call"]["placed_at"]
    assert result["call"]["outcome"] is None


# ---------------------------------------------------------------------------
# The published assistant's template variables
# ---------------------------------------------------------------------------


def test_every_declared_variable_is_filled(paths, monkeypatch):
    """A key missing from variableValues is rendered to the client verbatim as
    ``{{clientName}}``, so the set is a contract, not a convenience."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk_visible")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_published")
    result = vapi_client.start_web_call(
        "C-1",
        client_name="Asha",
        amount=1500,
        last_activity="2026-08-20",
        voice_path=paths["voice"],
        audit_path=paths["audit"],
    )
    variables = result["web"]["assistantOverrides"]["variableValues"]
    assert set(variables) == {"clientName", "caseId", "amountDue", "lastActivity"}
    assert variables["clientName"] == "Asha"
    assert variables["caseId"] == "C-1"
    # The value is spoken verbatim by the voice provider, so it carries its own
    # currency word — a bare "1,500" was read out as "$1,500.00".
    assert variables["amountDue"] == "1,500 rupees"
    assert all(isinstance(value, str) for value in variables.values())


def test_missing_case_details_still_produce_speakable_variables():
    variables = vapi_client.variable_values(case_id="C-2", client_name="", amount=None, last_activity="")
    assert variables["clientName"] == "there"
    assert variables["amountDue"] == "the amount on file"
    assert variables["lastActivity"] == "not recorded"


# ---------------------------------------------------------------------------
# The follow-up email decision
# ---------------------------------------------------------------------------


def _sendable_case(paths, client_id="C-1"):
    """Seed a live, sendable case through the audit store of record.

    This deliberately does not use :func:`_write_audit`. The CSV is only a
    projection: the first ``log_event`` of the run regenerates it from SQLite and
    would silently erase a hand-written row, which is exactly what happens on the
    closing path because ``record_call_audit`` writes before the email decision.
    Seeding through ``log_event`` is the only way the case survives to be found.

    ``RecoveryService.list_clients`` decides whether a case exists at all, so the
    action must be in ``CASE_ACTIONS`` and a client name must resolve, or the row
    is skipped outright and the send reports ``case_not_found``.
    """
    from modules.audit_log import log_event

    log_event(
        {
            "client_id": client_id,
            "client_name": "Asha",
            "client_email": "asha@example.com",
            "client_phone": "+919000000000",
            "amount": 1500,
        },
        "resend_payment_link",
        "Seeded case.",
        "link_created",
        paths["audit"],
    )


@pytest.mark.parametrize("outcome", ["declined", "escalated", "no_answer"])
def test_only_a_captured_promise_can_ever_send(paths, outcome):
    """The gate is deterministic and comes before the model, so no verdict from
    a model can cause a send on a call that did not capture a promise."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    decision = follow_up_email_for_call(
        call,
        {"outcome": outcome},
        transcript="anything at all",
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        email_caller=_never_called,
    )
    assert decision["should_send"] is False
    assert decision["sent"] is False
    assert decision["blocked_by"] == "outcome"


def test_switching_auto_email_off_records_the_promise_without_a_link(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    decision = follow_up_email_for_call(
        call,
        {"outcome": "promised_to_pay", "promise_date": "2026-09-04"},
        transcript="Yes, send me the link.",
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        auto_email=False,
        email_caller=_never_called,
    )
    assert decision["sent"] is False
    assert decision["blocked_by"] == "auto_email_disabled"


def test_the_model_can_veto_a_send_on_a_promise(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    decision = follow_up_email_for_call(
        call,
        {"outcome": "promised_to_pay"},
        transcript="I will pay in person at reception tomorrow, no email please.",
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        email_caller=_email_caller(send=False),
    )
    assert decision["should_send"] is False
    assert decision["blocked_by"] == "agent_declined"


def test_a_promise_with_no_live_case_is_reported_not_raised(paths):
    call = open_call("GONE", mode="web", path=paths["voice"])
    _write_audit(paths["audit"], [{"timestamp": _iso(-1)}])
    decision = follow_up_email_for_call(
        call,
        {"outcome": "promised_to_pay"},
        transcript="Yes, email me the link.",
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        email_caller=_email_caller(send=True),
    )
    assert decision["should_send"] is True
    assert decision["sent"] is False
    assert decision["blocked_by"] == "case_not_found"


def test_an_unreachable_model_still_sends_the_promised_link(paths):
    """A captured promise is the operator's commitment. If no model can be
    reached to second-guess it, the link goes out."""

    def unreachable(_briefing):
        raise RuntimeError("no provider configured")

    decision = decide_follow_up_email("Yes, tomorrow.", {"outcome": "promised_to_pay"}, unreachable)
    assert decision["should_send"] is True
    assert decision["source"] == "default"


def test_a_verdict_outside_the_contract_is_refused():
    with pytest.raises(VoiceOutcomeError):
        validate_email_decision({"send_link": "maybe"})
    with pytest.raises(VoiceOutcomeError):
        validate_email_decision({})
    assert validate_email_decision({"send_link": "yes"})["should_send"] is True
    assert validate_email_decision({"send_link": False})["should_send"] is False


def test_a_sent_link_is_audited_as_the_voice_agent(paths):
    from modules.audit_log import read_events

    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    gmail = _Gmail()
    decision = follow_up_email_for_call(
        call,
        {"outcome": "promised_to_pay", "promise_date": "2026-09-04"},
        transcript="Yes, email me the link.",
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        email_caller=_email_caller(send=True),
        payment_client=_PaymentClient(),
        message_service=gmail,
    )
    assert decision["sent"] is True
    assert decision["short_url"] == "https://pay.test/voice"
    # The link is only real if it actually left through the delivery provider.
    assert gmail.users().messages().body is not None

    row = next(event for event in read_events(paths["audit"]) if event["action"] == VOICE_LINK_ACTION)
    assert row["outcome"] == VOICE_LINK_OUTCOME
    assert row["actor"] == "voice_agent"
    # The masked outcome is the whole attribution defence; assert it on the row
    # that was actually written rather than trusting the constant.
    assert latest_email_sent_at("C-1", paths["audit"], paths["attempts"]) is None


def test_closing_a_call_returns_the_email_decision_to_the_browser(paths):
    """The panel renders ``result.email``; a closing path that omits it would
    leave the operator unable to tell whether a link went out."""
    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="Yes I will pay tomorrow",
        speech_detected=True,
        seconds_to_first_speech=1.0,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        caller=_promise_caller(),
        auto_email=True,
        email_caller=_email_caller(send=True),
        payment_client=_PaymentClient(),
        message_service=_Gmail(),
    )
    assert result["classification"]["outcome"] == "promised_to_pay"
    assert result["email"]["sent"] is True
    assert result["email"]["should_send"] is True


# ---------------------------------------------------------------------------
# Saying "yes" to the offered plan emails the plan, end to end
# ---------------------------------------------------------------------------


def test_a_yes_to_the_offered_plan_emails_the_plan_link(paths):
    """The user's requested flow, driven through the real closing path.

    The agent asks whether a customised plan is wanted, the client says only
    "haan, bhej do", and that one word has to be enough to (a) count as the
    request, (b) send the plan invite, and (c) stop the full-amount follow-up.
    Nothing in the transcript mentions money or installments, so this fails the
    moment ``accepted_plan_offer`` stops reading the agent's offer for context.
    """
    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    gmail = _Gmail()
    result = vapi_client.complete_web_call(
        call["id"],
        transcript=(
            "Agent: We can arrange a customised payment plan for you. "
            "Shall I email you the link to set it up?\n"
            "Client: हाँ, भेज दो."
        ),
        speech_detected=True,
        seconds_to_first_speech=1.0,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        plan_path=paths["plans"],
        caller=_declined_caller(),
        auto_email=True,
        # The follow-up model must never be reached: the plan diverts the send
        # before any verdict on the full amount can be asked for.
        email_caller=_never_called,
        message_service=gmail,
    )
    assert result["plan"]["requested"] is True
    assert result["plan"]["invited"] is True
    assert result["email"]["sent"] is False
    assert result["email"]["blocked_by"] == "flexible_plan_requested"
    # One email left, and it was the plan invitation rather than a demand.
    assert gmail.users().messages().body is not None


def test_the_plan_request_is_audited_with_the_client_s_own_words(paths):
    """The row explaining why the case stopped being chased must quote them."""
    from modules.audit_log import read_events

    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    vapi_client.complete_web_call(
        call["id"],
        transcript=(
            "Agent: Namaste Aditya ji, Naina bol rahi hoon.\n"
            "Client: अभी पैसे नहीं हैं\n"
            "Agent: Main samajh sakti hoon.\n"
            "Client: कोई और plan है आपके पास?"
        ),
        speech_detected=True,
        seconds_to_first_speech=1.0,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        plan_path=paths["plans"],
        caller=_declined_caller(),
        auto_email=True,
        email_caller=_never_called,
        message_service=_Gmail(),
    )
    row = next(event for event in read_events(paths["audit"]) if event["outcome"] == "plan_requested")
    assert row["actor"] == "voice_agent"


def test_a_call_nobody_answered_is_never_sent_a_plan(paths):
    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="",
        speech_detected=False,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        plan_path=paths["plans"],
        auto_email=True,
        email_caller=_never_called,
        plan_caller=_never_called,
        message_service=_Gmail(),
    )
    assert result["call"]["outcome"] == "no_answer"
    assert result["plan"]["requested"] is False
    assert result["plan"]["invited"] is False


def test_the_kill_switch_records_the_plan_request_without_emailing_it(paths):
    """``VOICE_AUTO_EMAIL`` off must still capture *why* the call ended there."""
    _sendable_case(paths)
    call = open_call("C-1", mode="web", path=paths["voice"])
    gmail = _Gmail()
    result = vapi_client.complete_web_call(
        call["id"],
        transcript="Client: koi aur plan hai aapke paas?",
        speech_detected=True,
        seconds_to_first_speech=1.0,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        plan_path=paths["plans"],
        caller=_declined_caller(),
        auto_email=False,
        email_caller=_never_called,
        message_service=gmail,
    )
    assert result["plan"]["requested"] is True
    assert result["plan"]["invited"] is False
    assert result["plan"]["blocked_by"] == "auto_email_disabled"
    assert gmail.users().messages().body is None


# ---------------------------------------------------------------------------
# The assistant reports its own outcome (logRecoveryOutcome)
# ---------------------------------------------------------------------------


def _tool_payload(call_id, arguments, *, name="logRecoveryOutcome", tool_call_id="tc-1", legacy=False, transcript=""):
    """Build one ``tool-calls`` delivery in either shape Vapi sends."""
    if legacy:
        entry = {"id": tool_call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}
        invocations = {"toolCalls": [entry]}
    else:
        entry = {"id": tool_call_id, "name": name, "arguments": arguments}
        invocations = {"toolCallList": [entry]}
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": "vapi-call-1", "metadata": {"call_log_id": call_id}},
            # The tool contract carries no final answer, so the transcript is the
            # only place that fact can come from on this path.
            "transcript": transcript,
            **invocations,
        }
    }


def _record_tool(paths, payload, **kwargs):
    return vapi_client.record_tool_outcome(
        payload,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        auto_email=False,
        **kwargs,
    )


def test_the_assistant_can_close_its_own_call_from_inside_the_conversation(paths):
    """A first-hand report needs no inference, so it is preferred over the
    after-the-fact classifier — the agent was there and the classifier was not."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _record_tool(
        paths,
        _tool_payload(call["id"], {"outcome": "promised_to_pay", "promise_date": "2026-09-04", "summary": "Client will pay Friday.", "confidence": 0.9}),
    )
    assert result["handled"] is True
    assert result["call"]["outcome"] == "promised_to_pay"
    assert result["call"]["promise_date"] == "2026-09-04"
    assert result["call"]["answered"] is True
    assert result["classification"]["source"] == "assistant-tool"


def test_the_older_openai_shaped_tool_call_is_read_too(paths):
    """Arguments arrive as a JSON string in the legacy shape; an assistant
    published against it must not silently stop reporting."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _record_tool(paths, _tool_payload(call["id"], {"outcome": "declined", "summary": "Refused."}, legacy=True))
    assert result["handled"] is True
    assert result["call"]["outcome"] == "declined"


def test_a_tool_call_is_always_acknowledged_even_when_it_is_unusable(paths):
    """The assistant is still mid-call and waiting. Withholding the result is
    what produces the dead air this work set out to remove, so every branch
    answers — and an outcome outside the contract still leaves the row open for
    the end-of-call report to classify properly."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _record_tool(paths, _tool_payload(call["id"], {"outcome": "no_answer"}))
    assert result["handled"] is False
    assert result["results"] == [{"toolCallId": "tc-1", "result": "recorded"}]
    assert get_call(call["id"], paths["voice"])["ended_at"] is None


def test_a_different_tool_is_left_alone(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _record_tool(paths, _tool_payload(call["id"], {"outcome": "declined"}, name="lookupSomethingElse"))
    assert result["handled"] is False
    assert get_call(call["id"], paths["voice"])["ended_at"] is None


def test_a_tool_report_after_the_row_closed_is_a_duplicate_not_an_error(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    _close(paths, call["id"], transcript="I will pay", speech_detected=True, seconds_to_first_speech=1.0)
    result = _record_tool(paths, _tool_payload(call["id"], {"outcome": "declined"}))
    assert result["handled"] is False
    assert result["duplicate"] is True
    assert result["results"][0]["toolCallId"] == "tc-1"


def test_the_webhook_returns_tool_results_to_the_provider(paths, monkeypatch):
    """Vapi reads ``results`` off the 200 response to unblock the assistant."""
    monkeypatch.setattr(vapi_client, "VOICE_DB_PATH", paths["voice"])
    call = open_call("C-1", mode="web", path=paths["voice"])
    payload = json.dumps(_tool_payload(call["id"], {"outcome": "declined", "summary": "Refused."})).encode()
    body, status = vapi_client.ingest_webhook(
        payload,
        {"X-Vapi-Secret": "s3cret"},
        secret="s3cret",
        voice_path=paths["voice"],
        audit_path=paths["audit"],
    )
    assert status == 200
    assert body["results"] == [{"toolCallId": "tc-1", "result": "recorded"}]


# ---------------------------------------------------------------------------
# The client's final answer — a separate typed question from the outcome
# ---------------------------------------------------------------------------
#
# The call that prompted this work: the client said "इसी और दिन" (some other
# day) and the dashboard recorded "will pay the 199 rupees today". Every test
# below exists to keep one of those two facts from becoming the other.


# The transcript verbatim from that call, trimmed to the turns that matter.
_ADITYA_TRANSCRIPT = "\n".join(
    [
        "Agent: नमस्ते, आदित्य जोशी.",
        "Agent: क्या आप इस एक सौ निन्यानवे रुपए का payment आज कर पाएंगे या किसी और दिन करना चाहेंगे?",
        "Client: भूल गया था.",
        "Agent: कोई बात नहीं, ऐसा हो जाता है.",
        "Client: इसी और दिन.",
    ]
)


def _final_answer_caller(**overrides):
    """Stand in for the final-answer model with a valid typed payload."""
    payload = {
        "kind": "paying_on_date",
        "answer": "The client will pay on another day.",
        "pay_date": None,
        "client_words": "इसी और दिन.",
        "confidence": 0.8,
        **overrides,
    }

    def caller(_briefing):
        return json.dumps(payload)

    return caller


def test_some_other_day_is_never_recorded_as_today():
    """The reported bug, pinned at the source.

    "इसी और दिन" is a commitment without a day. Reporting it as *today* is not a
    rounding error — it is the difference between an operator waiting and an
    operator following up.
    """
    answer = heuristic_final_answer(_ADITYA_TRANSCRIPT, {"outcome": "promised_to_pay"})
    assert answer["kind"] == "paying_on_date"
    assert answer["pay_date"] is None
    assert answer["client_words"] == "इसी और दिन."


def test_a_later_commitment_beats_an_earlier_mention_of_today():
    """A client who says "today" and then "no, some other day" has said the
    second thing. Reading the first is how the summary got it backwards."""
    transcript = "Client: आज कर देता हूं.\nClient: नहीं, किसी और दिन."
    assert heuristic_final_answer(transcript)["kind"] == "paying_on_date"


def test_the_agent_s_own_proposal_is_never_the_client_s_answer():
    """The agent offered "आज"; the client committed to nothing. The column must
    say so rather than adopting the agent's suggestion as a fact."""
    transcript = "Agent: आज कर पाएंगे? अभी link भेज दूं?\nClient: हम्म."
    answer = heuristic_final_answer(transcript)
    assert answer["kind"] == "unclear"
    assert answer["client_words"] == "हम्म."


def test_paying_immediately_resolves_to_a_real_date():
    answer = heuristic_final_answer("Client: मैं आज ही payment कर देता हूं.")
    assert answer["kind"] == "paying_now"
    assert answer["pay_date"] == datetime.now(timezone.utc).date().isoformat()


def test_a_refusal_and_a_complaint_are_told_apart():
    assert heuristic_final_answer("Client: मैं payment नहीं करूंगा.")["kind"] == "refused"
    assert heuristic_final_answer("Client: मुझे शिकायत करनी है.")["kind"] == "needs_human"
    assert heuristic_final_answer("Client: I already paid this.")["kind"] == "refused"


def test_a_call_with_no_client_speech_reports_unclear_not_a_promise():
    """An empty transcript is not a commitment, even on a call the classifier
    labelled ``promised_to_pay``."""
    answer = heuristic_final_answer("Agent: नमस्ते.", {"outcome": "promised_to_pay"})
    assert answer["kind"] == "unclear"
    assert answer["client_words"] == ""


def test_every_final_answer_kind_is_inside_the_closed_set():
    assert set(FINAL_ANSWER_KINDS) == {"paying_now", "paying_on_date", "refused", "needs_human", "unclear"}
    # The final answer is its own question. Widening the outcome enum with it
    # would silently change every metric card that counts outcomes.
    assert not set(FINAL_ANSWER_KINDS) & set(OUTCOMES)


def test_an_invented_date_is_dropped_rather_than_repaired():
    answer = validate_final_answer({"kind": "paying_on_date", "pay_date": "next Friday", "confidence": 0.9})
    assert answer["pay_date"] is None
    assert answer["answer"]


def test_a_date_is_meaningless_on_a_refusal_and_is_cleared():
    for kind in ("refused", "needs_human", "unclear"):
        assert validate_final_answer({"kind": kind, "pay_date": "2026-09-04"})["pay_date"] is None


def test_a_final_answer_kind_outside_the_contract_is_refused():
    with pytest.raises(VoiceOutcomeError):
        validate_final_answer({"kind": "will_think_about_it"})
    with pytest.raises(VoiceOutcomeError):
        validate_final_answer("paying_now")


def test_an_impossible_confidence_is_normalized_not_trusted():
    assert validate_final_answer({"kind": "paying_now", "confidence": 5.0})["confidence"] == 0.5
    assert validate_final_answer({"kind": "paying_now", "confidence": "high"})["confidence"] == 0.5


def test_a_model_that_summarises_still_gets_the_client_quoted():
    """The column promises the client's own words. A model that paraphrases
    instead of quoting must not turn that into 'the client said nothing'."""
    answer = extract_final_answer(_ADITYA_TRANSCRIPT, None, _final_answer_caller(client_words=""))
    assert answer["source"] == "llm"
    assert answer["client_words"] == "इसी और दिन."


def test_an_unreachable_model_still_fills_the_column():
    def exploding(_briefing):
        raise RuntimeError("provider down")

    answer = extract_final_answer(_ADITYA_TRANSCRIPT, {"outcome": "promised_to_pay"}, exploding)
    assert answer["source"] == "heuristic"
    assert answer["kind"] == "paying_on_date"


def test_a_final_answer_outside_the_contract_falls_back_instead_of_leaking():
    def rogue(_briefing):
        return json.dumps({"kind": "definitely_paying", "pay_date": "whenever"})

    assert extract_final_answer("Client: इसी और दिन.", None, rogue)["source"] == "heuristic"


def test_an_unanswered_call_has_no_final_answer_and_consults_no_model():
    resolved = resolve_call_outcome(
        answered=False,
        transcript="",
        caller=_never_called,
        final_answer_caller=_never_called,
    )
    # Not an empty answer — no answer. Nobody was on the line to have one.
    assert resolved["final_answer"] is None


def test_the_final_answer_lands_in_the_same_write_as_the_outcome(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    closed = close_call(
        call["id"],
        outcome="promised_to_pay",
        answered=True,
        promise_date="2026-09-04",
        final_answer={"kind": "paying_on_date", "answer": "Another day.", "pay_date": "2026-09-04", "client_words": "इसी और दिन."},
        path=paths["voice"],
    )
    assert closed["final_answer_kind"] == "paying_on_date"
    assert closed["client_final_words"] == "इसी और दिन."
    stored = get_call(call["id"], paths["voice"])
    assert stored["final_answer"] == "Another day."
    assert stored["final_pay_date"] == "2026-09-04"


def test_a_stored_final_answer_kind_outside_the_enum_is_refused(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    with pytest.raises(VoiceOutcomeError):
        close_call(call["id"], outcome="declined", answered=True, final_answer={"kind": "maybe"}, path=paths["voice"])


def test_an_unanswered_row_stores_no_final_answer_even_if_one_is_passed(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    closed = close_call(
        call["id"],
        outcome="no_answer",
        answered=False,
        final_answer={"kind": "paying_now", "client_words": "अभी कर देता हूं."},
        path=paths["voice"],
    )
    assert closed["final_answer_kind"] == ""
    assert closed["client_final_words"] == ""


def test_the_columns_are_empty_strings_rather_than_nulls(paths):
    """The dashboard renders these into a cell directly; "" is the honest value
    for a call that has not been classified yet."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    for key in ("final_answer_kind", "final_answer", "final_pay_date", "client_final_words"):
        assert call[key] == ""


def test_the_browser_path_records_what_the_client_finally_said(paths):
    """End to end on the exact call that was misreported: the outcome is still
    ``promised_to_pay``, and the final answer no longer says 'today'."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(paths, call["id"], transcript=_ADITYA_TRANSCRIPT, speech_detected=True, seconds_to_first_speech=1.0)
    assert result["call"]["outcome"] == "promised_to_pay"
    assert result["call"]["final_answer_kind"] == "paying_on_date"
    assert result["call"]["final_pay_date"] is None or result["call"]["final_pay_date"] == ""
    assert result["call"]["client_final_words"] == "इसी और दिन."
    # The browser needs it in the response too, to show the operator the row it
    # just created without waiting for a refresh.
    assert result["classification"]["final_answer"]["kind"] == "paying_on_date"


def test_a_call_the_assistant_closed_itself_still_reports_the_final_answer(paths):
    """The tool contract carries an outcome and no final answer, so this path has
    to extract it from the transcript or the column would be blank for every
    call the assistant closed from inside the conversation."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _record_tool(
        paths,
        _tool_payload(
            call["id"],
            {"outcome": "promised_to_pay", "summary": "Client will pay later.", "confidence": 0.9},
            transcript=_ADITYA_TRANSCRIPT,
        ),
    )
    assert result["handled"] is True
    assert result["call"]["final_answer_kind"] == "paying_on_date"
    assert result["call"]["client_final_words"] == "इसी और दिन."


def test_a_server_closed_call_records_the_final_answer_too(paths):
    """A shut browser tab or an outbound phone call is closed by the webhook.
    Those are exactly the rows an operator cannot re-read, so they need the
    column most."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "vapi-call-1", "metadata": {"call_log_id": call["id"]}},
            "endedReason": "customer-ended-call",
            "transcript": _ADITYA_TRANSCRIPT,
        }
    }
    result = vapi_client.normalize_end_of_call(
        payload,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        caller=_promise_caller(),
        final_answer_caller=_final_answer_caller(),
        auto_email=False,
    )
    assert result["handled"] is True
    assert result["call"]["final_answer_kind"] == "paying_on_date"


def test_a_no_answer_report_leaves_the_column_empty(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "vapi-call-1", "metadata": {"call_log_id": call["id"]}},
            "endedReason": "customer-did-not-answer",
        }
    }
    result = vapi_client.normalize_end_of_call(
        payload,
        voice_path=paths["voice"],
        audit_path=paths["audit"],
        attempts_path=paths["attempts"],
        caller=_never_called,
        final_answer_caller=_never_called,
        auto_email=False,
    )
    assert result["call"]["outcome"] == "no_answer"
    assert result["call"]["final_answer_kind"] == ""


def test_the_history_dropdown_carries_the_final_answer_through(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    _close(paths, call["id"], transcript=_ADITYA_TRANSCRIPT, speech_detected=True, seconds_to_first_speech=1.0)
    history = call_history("C-1", paths["voice"], paths["audit"])
    assert history[0]["final_answer_kind"] == "paying_on_date"
    assert history[0]["client_final_words"] == "इसी और दिन."


# ---------------------------------------------------------------------------
# Nobody is hung up on without being thanked
# ---------------------------------------------------------------------------
#
# All three hang-up guarantees are substring matches against the agent's spoken
# text. Every phrase used to be English, so on a Hindi call the agent said
# "धन्यवाद", matched nothing, ended nothing — and the transcript stopped on the
# agent's own unanswered question.


def test_the_closing_line_is_spoken_in_both_languages():
    assert "धन्यवाद" in vapi_client.END_CALL_MESSAGE
    assert "Thank you" in vapi_client.END_CALL_MESSAGE


def test_the_farewell_list_is_not_blind_in_hindi():
    phrases = vapi_client.END_CALL_PHRASES
    assert "आपका दिन शुभ हो" in phrases
    assert "अलविदा" in phrases
    # Transliterations matter separately: the transcriber may return Roman script
    # for Hindi speech, in which case the Devanagari entries never match.
    assert "alvida" in phrases
    assert "aapka din shubh ho" in phrases


def test_every_farewell_phrase_is_lowercased_for_matching():
    """Matching lowercases the transcript line, so an uppercase entry here is a
    phrase that can never fire."""
    assert all(phrase == phrase.lower() for phrase in vapi_client.END_CALL_PHRASES)


def test_the_agent_s_own_closing_line_trips_the_farewell_watch():
    """The three guarantees would not be three if the message the agent is told
    to say were not in the list the browser and the provider watch for."""
    spoken = vapi_client.END_CALL_MESSAGE.lower()
    assert any(phrase in spoken for phrase in vapi_client.END_CALL_PHRASES)


def test_the_prompt_forbids_ending_on_a_question():
    prompt = vapi_client.ASSISTANT_SYSTEM_PROMPT
    assert "Never end on a question" in prompt
    # The exact line, so the prompt and the phrase list cannot drift apart.
    assert vapi_client.END_CALL_MESSAGE in prompt
    assert "Thank the client before you hang up" in prompt


def test_an_inline_assistant_cannot_be_built_without_the_hang_up_guarantees(monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    assistant = vapi_client.build_assistant(vapi_client.vapi_config())["assistant"]
    assert assistant["endCallFunctionEnabled"] is True
    assert assistant["endCallMessage"] == vapi_client.END_CALL_MESSAGE
    assert "अलविदा" in assistant["endCallPhrases"]
    assert "धन्यवाद" not in assistant["endCallPhrases"]


def test_a_dashboard_authored_assistant_has_the_same_guarantees_forced_on_it(monkeypatch):
    """Hanging up is behaviour this project guarantees rather than delegates, so
    an operator cannot publish an assistant that leaves the line open."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    overrides = vapi_client.build_assistant(vapi_client.vapi_config())["assistantOverrides"]
    assert overrides["endCallFunctionEnabled"] is True
    assert overrides["endCallMessage"] == vapi_client.END_CALL_MESSAGE
    assert "अलविदा" in overrides["endCallPhrases"]
    assert "धन्यवाद" not in overrides["endCallPhrases"]


def test_the_browser_is_handed_the_farewell_it_has_to_watch_for(paths, monkeypatch):
    """The browser's transcript watch is the third guarantee. Server-owned so it
    can never disagree with what the provider matches on."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    web = vapi_client.start_web_call("C-1", voice_path=paths["voice"], audit_path=paths["audit"])["web"]
    assert web["end_call_message"] == vapi_client.END_CALL_MESSAGE
    assert web["end_call_phrases"] == vapi_client.terminal_phrases()
    assert web["end_call_grace_seconds"] > 0


# ---------------------------------------------------------------------------
# A greeting is not a goodbye
# ---------------------------------------------------------------------------
#
# The same substring matching that makes the Hindi farewells work cannot tell an
# opening "नमस्ते" from a closing one. Listing it as a farewell ended the call on
# the agent's own first sentence: all three guarantees fired at once and the
# client was hung up on before they had spoken a word.


def test_the_agent_s_greeting_is_never_a_farewell():
    assert vapi_client.is_farewell("नमस्ते, आदित्य जोशी.") is False
    assert vapi_client.is_farewell("Namaste Aditya, main Naina bol rahi hoon.") is False
    assert vapi_client.is_farewell("Hello, is this Aditya?") is False


def test_a_real_closing_still_trips_the_watch():
    assert vapi_client.is_farewell("धन्यवाद, आपका दिन शुभ हो.") is True
    assert vapi_client.is_farewell("Thanks for your time. Goodbye.") is True
    # The exact line the agent is told to say, which is what actually ends calls.
    assert vapi_client.is_farewell(vapi_client.END_CALL_MESSAGE) is True


# ---------------------------------------------------------------------------
# Courtesy is not a closing
# ---------------------------------------------------------------------------
#
# A live call died two sentences into the pitch. The client said "बोलिए" ("go
# ahead"), the agent thanked them for it — "धन्यवाद. दरअसल, आपके account पर..." —
# and a bare "धन्यवाद" was in the farewell list. All three hang-up guarantees
# fired on the politest word in the language.
#
# The fix is not a longer blocklist. It is that a farewell has to be TERMINAL:
# the utterance must END on it, and a lone word can only qualify if it means
# nothing except that the call is over.

_MID_CALL_COURTESY = [
    "धन्यवाद. दरअसल, आपके account पर एक सौ निन्यानवे रुपए का payment pending है.",
    "धन्यवाद. क्या आप आज payment कर सकते हैं?",
    "शुक्रिया. मैं आपको link भेज देती हूं.",
    "Dhanyavaad. Aapka payment pending hai.",
    "Thank you. Can you pay by Friday?",
    "Thanks, I will email you the link now.",
    "Okay, take care of that today and we are done.",
    "I will follow up with an email in a minute.",
]


@pytest.mark.parametrize("line", _MID_CALL_COURTESY)
def test_ordinary_politeness_never_ends_the_call(line):
    assert vapi_client.is_farewell(line) is False


def test_the_courtesy_word_that_cut_the_live_call_is_not_published():
    published = vapi_client.terminal_phrases()
    assert "धन्यवाद" not in published
    assert "dhanyavaad" not in published
    assert "dhanyawad" not in published


def test_a_courtesy_word_cannot_be_added_back_to_the_list():
    """The blocklist is enforcement, not documentation: a courtesy word typed
    into the Vapi dashboard is filtered before it can end a call."""
    assert vapi_client.terminal_phrases(["धन्यवाद", "thank you", "shukriya"]) == []
    assert vapi_client.terminal_phrases(["धन्यवाद", "अलविदा"]) == ["अलविदा"]


def test_no_published_phrase_is_an_ambiguous_single_word():
    """One word gets no context to disambiguate it, so only the closed set of
    pure leave-taking words is allowed to stand alone."""
    for phrase in vapi_client.terminal_phrases():
        if len(phrase.split()) == 1:
            assert phrase in vapi_client.CLOSING_WORDS, phrase


def test_a_farewell_the_agent_talks_past_is_not_a_farewell():
    """Terminal means terminal. Saying goodbye and then continuing is a figure of
    speech, and hanging up on it truncates the call the client is still in."""
    assert vapi_client.is_farewell("Goodbye is not what I meant, sorry.") is False
    assert vapi_client.is_farewell("Goodbye.") is True


def test_punctuation_around_the_closing_does_not_hide_it():
    assert vapi_client.is_farewell("अलविदा।") is True
    assert vapi_client.is_farewell("  Goodbye!  ") is True
    assert vapi_client.is_farewell("Have a nice day...") is True


def test_the_prompt_reserves_the_closing_words_for_the_closing():
    """Matching is terminal now, but a model that says "goodbye" mid-sentence and
    then stops still ends the call — so the prompt fences the word off too."""
    prompt = vapi_client.ASSISTANT_SYSTEM_PROMPT
    assert "Never say any of them mid-call" in prompt


def test_no_greeting_survives_into_the_published_farewell_list():
    """The published list is the only one that reaches the provider or the
    browser, so the greeting block is enforcement rather than a comment."""
    published = " ".join(vapi_client.terminal_phrases()).lower()
    for greeting in vapi_client.GREETING_PHRASES:
        assert greeting.lower() not in published


def test_a_greeting_added_to_the_farewell_list_is_dropped_before_publication():
    assert "नमस्ते" not in vapi_client.terminal_phrases(["नमस्ते", "अलविदा"])
    assert "अलविदा" in vapi_client.terminal_phrases(["नमस्ते", "अलविदा"])


def test_the_browser_is_handed_the_openings_it_must_not_hang_up_on(paths, monkeypatch):
    """The browser matches substrings too, so it needs both lists or its own
    guarantee fires where the server's would not."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    web = vapi_client.start_web_call("C-1", voice_path=paths["voice"], audit_path=paths["audit"])["web"]
    assert web["greeting_phrases"] == list(vapi_client.GREETING_PHRASES)
    assert "नमस्ते" in web["greeting_phrases"]


def test_a_greeting_cannot_reach_the_provider_through_the_dashboard(monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    overrides = vapi_client.build_assistant(vapi_client.vapi_config())["assistantOverrides"]
    assert "नमस्ते" not in overrides["endCallPhrases"]
    assert "namaste" not in overrides["endCallPhrases"]


# ---------------------------------------------------------------------------
# Only the agent spoke, so nobody answered
# ---------------------------------------------------------------------------
#
# A call cut short during the greeting still leaves the agent's two lines behind.
# Reading those as a conversation is what filed an empty call as answered — a
# 100% answer rate and an ``escalated`` outcome describing a client who was never
# on the line.

_AGENT_ONLY = "Agent: नमस्ते, आदित्य जोशी.\nAgent: मैं नैना बोल रही हूं Razorpay recovery team से."


def test_a_transcript_of_only_agent_turns_is_recognised():
    assert agent_only_transcript(_AGENT_ONLY) is True


def test_one_client_turn_is_enough_to_be_a_conversation():
    assert agent_only_transcript(_AGENT_ONLY + "\nClient: हां बोलिए.") is False


def test_an_unattributed_transcript_keeps_its_power_as_evidence():
    """Whose speech it is cannot be established, so only a provably one-sided
    call loses the transcript's standing in step 1."""
    assert agent_only_transcript("Yes I will pay tomorrow") is False
    assert agent_only_transcript("") is False


def test_a_call_cut_off_during_the_greeting_is_no_answer(paths):
    """The exact call from the dashboard: two agent lines, no client, hung up on
    its own greeting. It used to close as ``escalated`` with a 100% answer rate."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(paths, call["id"], transcript=_AGENT_ONLY, speech_detected=False)
    assert result["call"]["answered"] is False
    assert result["call"]["outcome"] == "no_answer"
    assert result["call"]["final_answer_kind"] == ""


def test_the_agent_s_own_speech_is_not_a_late_answer(paths):
    """Timing cannot rescue it either: `speech_detected` is client-only, so an
    agent-only transcript with no client signal stays unanswered."""
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(
        paths,
        call["id"],
        transcript=_AGENT_ONLY,
        speech_detected=True,
        seconds_to_first_speech=vapi_client.SILENCE_WINDOW_SECONDS + 1,
    )
    assert result["call"]["answered"] is False


def test_a_client_who_spoke_in_time_is_answered_even_on_a_short_call(paths):
    call = open_call("C-1", mode="web", path=paths["voice"])
    result = _close(
        paths,
        call["id"],
        transcript=_AGENT_ONLY + "\nClient: I will pay tomorrow.",
        speech_detected=True,
        seconds_to_first_speech=1.0,
    )
    assert result["call"]["answered"] is True
    assert result["call"]["outcome"] == "promised_to_pay"


def test_the_webhook_path_refuses_an_agent_only_transcript_too():
    """Both closing paths ask step 1 the same question."""
    assert answered_from_ended_reason("customer-ended-call", _AGENT_ONLY) is False
    assert answered_from_ended_reason("customer-ended-call", _AGENT_ONLY + "\nClient: हां.") is True


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
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    result = vapi_client.start_web_call("C-1", client_name="Asha", amount=500, voice_path=paths["voice"], audit_path=paths["audit"])
    serialized = json.dumps(result)
    assert result["web"]["public_key"] == "pk_visible"
    assert "sk_must_not_leak" not in serialized
    # The row id travels in metadata so the server-push report can find it.
    assert result["web"]["metadata"]["call_log_id"] == result["call"]["id"]


# ---------------------------------------------------------------------------
# "I have no money" is an opening for a plan, not a refusal
#
# The failure these cover was end to end: a client who said they had no money
# was classified as a flat refusal, so no plan was requested, the full-amount
# link was blocked for a declined outcome, the client received nothing at all,
# and the agent hung up on them. Inability to pay in full and refusal to pay
# are different answers and must stay different here.
# ---------------------------------------------------------------------------


_NO_MONEY = (
    "Agent: Can you clear the balance today?\nClient: I have no money right now.",
    "Agent: Aaj payment ho jayega?\nClient: अभी पैसे नहीं हैं",
    "Agent: Aaj payment ho jayega?\nClient: madam abhi paise nahi hain",
)

_FLAT_REFUSALS = (
    "Agent: Can you pay today?\nClient: I am not paying, this is not my bill.",
    "Agent: Can you pay today?\nClient: I already paid this.",
    "Agent: Can you pay today?\nClient: Wrong number, not interested.",
    "Agent: Aaj payment?\nClient: मैं payment नहीं करूंगा.",
)


@pytest.mark.parametrize("transcript", _NO_MONEY)
def test_having_no_money_is_read_as_asking_for_a_plan(transcript):
    assert heuristic_plan_request(transcript)["requested"] is True


@pytest.mark.parametrize("transcript", _NO_MONEY)
def test_having_no_money_is_never_filed_as_a_refusal(transcript):
    assert heuristic_final_answer(transcript)["kind"] != "refused"


@pytest.mark.parametrize("transcript", _FLAT_REFUSALS)
def test_a_refusal_that_never_mentions_money_is_still_a_refusal(transcript):
    assert heuristic_final_answer(transcript)["kind"] == "refused"
    assert heuristic_plan_request(transcript)["requested"] is False


def test_an_escalation_still_outranks_the_plan_reading():
    transcript = "Agent: Can you pay?\nClient: I want a manager, this is harassment."
    assert heuristic_final_answer(transcript)["kind"] == "needs_human"


def test_the_client_is_quoted_asking_for_the_plan():
    """The quote becomes the chatbot's opening hint, so it must be the client's
    own sentence rather than a summary of it."""
    request = heuristic_plan_request(_NO_MONEY[0])
    assert "no money" in request["client_words"].lower()


def test_an_agent_only_call_still_asks_for_nothing():
    result = detect_plan_request(
        "Agent: Namaste.\nAgent: Are you there?",
        caller=lambda _: pytest.fail("a model must not be consulted here"),
    )
    assert result["requested"] is False
    assert result["source"] == "no_client_speech"


# ---------------------------------------------------------------------------
# Asking what else is available is asking for a plan
#
# From the live call that exposed this: the client said "कोई और plan है आपके
# पास?" — bare "plan", code-switched, with no money figure and none of the
# installment vocabulary. Nothing matched, so the request was missed and the
# customer was emailed nothing. A client asking what else you can do has asked.
# ---------------------------------------------------------------------------


_ASKED_WHAT_ELSE = (
    "Agent: Can you clear it today?\nClient: कोई और plan है आपके पास?",
    "Agent: Can you clear it today?\nClient: koi aur plan hai aapke paas?",
    "Agent: Can you clear it today?\nClient: Is there another plan you can offer?",
    "Agent: Can you clear it today?\nClient: any other option?",
    "Agent: Can you clear it today?\nClient: कोई और तरीका है?",
)


@pytest.mark.parametrize("transcript", _ASKED_WHAT_ELSE)
def test_asking_what_else_is_available_is_a_plan_request(transcript):
    assert heuristic_plan_request(transcript)["requested"] is True


@pytest.mark.parametrize("transcript", _ASKED_WHAT_ELSE)
def test_asking_what_else_is_available_is_never_a_refusal(transcript):
    assert heuristic_final_answer(transcript)["kind"] != "refused"


def test_the_live_call_that_was_missed_now_asks_for_a_plan():
    """Aditya Joshi's ₹199 call, in the order it was actually spoken."""
    transcript = (
        "Agent: Namaste Aditya ji, Naina bol rahi hoon.\n"
        "Client: अभी पैसे नहीं हैं\n"
        "Agent: Main samajh sakti hoon.\n"
        "Client: कोई और plan है आपके पास?"
    )
    request = heuristic_plan_request(transcript)
    assert request["requested"] is True
    # The quote must be the client's own sentence, because it becomes the
    # chatbot's opening line in the email they receive.
    assert request["client_words"]


def test_the_plan_wording_does_not_turn_a_refusal_into_a_request():
    """"I am cancelling my plan" is a churn statement, not a request to split."""
    assert (
        heuristic_plan_request(
            "Agent: Can you pay today?\nClient: No. I am cancelling and I already paid."
        )["requested"]
        is False
    )


# ---------------------------------------------------------------------------
# Saying "yes" to the offered plan IS the request
#
# The agent is instructed to ask whether a customised plan is wanted, so on a
# well-run call the client never has to find the vocabulary — they just say
# "yes". Read from the client's turns alone that is indistinguishable from
# agreeing to pay in full, so only the agent's preceding question separates
# them.
# ---------------------------------------------------------------------------


_OFFER = "Agent: We can arrange a customised payment plan for you. Shall I email you the link to set it up?"

_ACCEPTED = (
    f"{_OFFER}\nClient: Yes please.",
    f"{_OFFER}\nClient: हाँ, भेज दो.",
    f"{_OFFER}\nClient: haan ji bhej do",
    f"{_OFFER}\nClient: ok",
    f"{_OFFER}\nClient: theek hai",
)


@pytest.mark.parametrize("transcript", _ACCEPTED)
def test_accepting_the_offered_plan_is_a_request(transcript):
    request = heuristic_plan_request(transcript)
    assert request["requested"] is True
    assert request["client_words"]


@pytest.mark.parametrize(
    "transcript",
    (
        f"{_OFFER}\nClient: No thanks.",
        f"{_OFFER}\nClient: नहीं चाहिए.",
        f"{_OFFER}\nClient: nahi, mat bhejo",
    ),
)
def test_declining_the_offered_plan_is_not_a_request(transcript):
    assert heuristic_plan_request(transcript)["requested"] is False


def test_a_yes_with_no_offer_behind_it_is_not_a_plan_request():
    """Agreeing to pay in full is a "yes" too, and must stay one."""
    assert (
        heuristic_plan_request("Agent: Can you pay by Friday?\nClient: Yes, I will.")["requested"]
        is False
    )


def test_the_agent_offering_a_plan_alone_is_not_a_request():
    assert heuristic_plan_request(_OFFER)["requested"] is False


def test_changing_their_mind_about_the_offer_is_respected():
    """The last thing the client said about the offer is the answer."""
    assert (
        heuristic_plan_request(
            f"{_OFFER}\nClient: Yes please.\nAgent: I'll send it.\nClient: No, don't send it."
        )["requested"]
        is False
    )


def test_accepted_plan_offer_quotes_the_client_verbatim():
    assert accepted_plan_offer(f"{_OFFER}\nClient: हाँ, भेज दो.") == "हाँ, भेज दो."
    assert accepted_plan_offer("Agent: Can you pay?\nClient: Yes.") == ""


# ---------------------------------------------------------------------------
# The prompt has to offer the plan before it closes
# ---------------------------------------------------------------------------


def test_the_prompt_offers_a_plan_instead_of_hanging_up_on_no_money():
    prompt = vapi_client.ASSISTANT_SYSTEM_PROMPT
    assert "customised payment plan" in prompt
    assert "Shall I email you the link to set it up?" in prompt
    # The one question the agent is allowed past its own question budget: the
    # user's complaint was that the call ended without ever being asked.
    assert "even if you have already asked two" in prompt
    # And the branch must still be gated on the client saying it first.
    assert "Never volunteer one to a client who has not said" in prompt


def test_the_prompt_still_refuses_to_argue_with_a_real_refusal():
    prompt = vapi_client.ASSISTANT_SYSTEM_PROMPT
    assert "accept it the\n  first time and end the call" in prompt
    assert "do not\n  offer a plan" in prompt


# ---------------------------------------------------------------------------
# The plan offer is shipped, not delegated
# ---------------------------------------------------------------------------
#
# The script above was only ever sent on the inline branch. With
# VAPI_ASSISTANT_ID set — which is how this is deployed — the assistant ran
# whatever prompt an operator typed into Vapi's dashboard, and that prompt had no
# flexible-plan branch at all. The live agent therefore never asked a client who
# said they had no money whether to email the plan link, and because
# accepted_plan_offer() keys on the offer's wording, detection had nothing to
# anchor on either. The same failure, twice, from one missing override.


def _dashboard_prompt(monkeypatch) -> str:
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    overrides = vapi_client.build_assistant(
        vapi_client.vapi_config(), case_id="C-1", client_name="Aditya", amount=199.0
    )["assistantOverrides"]
    return overrides["model"]["messages"][0]["content"]


def _inline_prompt(monkeypatch) -> str:
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    assistant = vapi_client.build_assistant(
        vapi_client.vapi_config(), case_id="C-1", client_name="Aditya", amount=199.0
    )["assistant"]
    return assistant["model"]["messages"][0]["content"]


def test_a_dashboard_authored_assistant_cannot_be_built_without_the_plan_script(monkeypatch):
    prompt = _dashboard_prompt(monkeypatch)
    assert "We can arrange a customised payment plan for you." in prompt
    assert "Shall I email you the link to set it up?" in prompt
    assert "I'll email you a secure link" in prompt


def test_both_branches_speak_the_same_script(monkeypatch):
    """One prompt, two transports. A guarantee that holds on only one of them is
    not a guarantee."""
    assert _dashboard_prompt(monkeypatch) == _inline_prompt(monkeypatch)


def test_the_forced_prompt_still_carries_the_case_facts(monkeypatch):
    """Overriding the dashboard prompt removes its {{mustache}} variables from the
    conversation, so the facts have to arrive as sentences instead."""
    prompt = _dashboard_prompt(monkeypatch)
    assert "Client name: Aditya." in prompt
    assert "199 rupees" in prompt
    # Spoken as a word, never as a symbol: the rupee sign was voiced as "dollars".
    assert "\u20b9" not in prompt


def test_the_forced_prompt_does_not_replace_the_template_variables(monkeypatch):
    """A dashboard first message may still reference {{clientName}}; overriding the
    model must not take the substitutions away from it."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    overrides = vapi_client.build_assistant(
        vapi_client.vapi_config(), case_id="C-1", client_name="Aditya", amount=199.0
    )["assistantOverrides"]
    assert overrides["variableValues"]["clientName"] == "Aditya"
    assert overrides["variableValues"]["amountDue"] == "199 rupees"


def test_the_plan_offer_stays_gated_on_a_money_shortage(monkeypatch):
    """The ask is for a client who said they cannot pay in full — never a blanket
    question, and never twice."""
    for prompt in (_dashboard_prompt(monkeypatch), _inline_prompt(monkeypatch)):
        assert "Never volunteer one to a client who has not said" in prompt
        assert "never offer one twice" in prompt
        assert "cannot pay the full amount" in prompt


def test_the_prompt_promises_an_emailed_chatbot_and_decides_nothing_itself():
    """The agent promises a link and stops. The schedule is somebody else's call."""
    script = vapi_client.PLAN_OFFER_INSTRUCTIONS
    assert "Never name an amount, never name a date" in script
    assert "you only promise the link" in script


def test_the_offer_the_prompt_dictates_is_the_offer_detection_recognises():
    """The two halves are coupled through wording: if the agent's offer turn does
    not match an offer hint, a client's "yes" is credited to nothing."""
    offer = "Agent: We can arrange a customised payment plan for you."
    question = "Agent: Shall I email you the link to set it up?"
    assert accepted_plan_offer(f"{offer}\nClient: Yes please.") == "Yes please."
    assert accepted_plan_offer(f"{question}\nClient: हाँ.") == "हाँ."
    assert heuristic_plan_request(f"{question}\nClient: Yes, send it.")["requested"] is True


# ---------------------------------------------------------------------------
# The payload the provider will actually accept
# ---------------------------------------------------------------------------
#
# Forcing the prompt onto the dashboard assistant was correct and still broke every
# live call: ``POST https://api.vapi.ai/call/web`` answered
#
#   {"message": ["assistantOverrides.model.maxTokens must not be less than 50"],
#    "error": "Bad Request", "statusCode": 400}
#
# and the browser SDK surfaced it as `start-method-error`. Nothing above caught it,
# because every assistant test asserts on the dictionary we build and none of them
# knows what the provider will take. These do: the numeric bounds below are copied
# from Vapi's own OpenAPI schema, and the assistant is checked against them the way
# the API checks it. A field the provider would reject now fails here first.
#
# Both branches are checked, because the same model block is sent inline and as an
# override, and a bound is only worth pinning if it holds everywhere it is used.
PROVIDER_BOUNDS: dict[str, tuple[float, float]] = {
    # OpenAIModel.maxTokens — the field that caused the 400.
    "maxTokens": (50, 10000),
    "temperature": (0, 2),
    # AssistantOverrides.maxDurationSeconds
    "maxDurationSeconds": (10, 43200),
    # StartSpeakingPlan / StopSpeakingPlan
    "waitSeconds": (0, 5),
    "numWords": (0, 10),
    "voiceSeconds": (0, 0.5),
    "backoffSeconds": (0, 10),
}


def _within_bounds(payload: dict[str, object]) -> list[str]:
    """Every bounded field in ``payload``, reported as a list of violations."""
    broken: list[str] = []
    for field, (low, high) in PROVIDER_BOUNDS.items():
        if field in payload and not low <= float(payload[field]) <= high:  # type: ignore[arg-type]
            broken.append(f"{field}={payload[field]} outside [{low}, {high}]")
    return broken


def test_the_model_block_respects_the_provider_s_own_limits():
    """`maxTokens: 40` was rejected outright. The floor is 50, and it is the
    provider's floor rather than a preference — no call happens below it."""
    model = vapi_client.model_settings(client_name="Aditya", amount=199.0)
    assert _within_bounds(model) == []
    assert model["maxTokens"] >= vapi_client.PROVIDER_MIN_MAX_TOKENS


def test_the_reply_cap_is_not_quietly_lowered_below_the_floor():
    """A future attempt to shorten replies must go through the prompt, not through
    a number the API refuses."""
    assert vapi_client.MODEL_MAX_TOKENS >= vapi_client.PROVIDER_MIN_MAX_TOKENS


@pytest.mark.parametrize("branch", ["dashboard", "inline"])
def test_every_number_sent_to_the_provider_is_inside_its_accepted_range(monkeypatch, branch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    if branch == "dashboard":
        monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    else:
        monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    built = vapi_client.build_assistant(vapi_client.vapi_config(), case_id="C-1", client_name="Aditya", amount=199.0)
    body = built.get("assistantOverrides") or built["assistant"]

    broken = _within_bounds(body)
    broken += _within_bounds(body["model"])
    broken += _within_bounds(body["startSpeakingPlan"])
    broken += _within_bounds(body["stopSpeakingPlan"])
    assert broken == []


@pytest.mark.parametrize("branch", ["dashboard", "inline"])
def test_the_farewell_list_fits_the_field_that_carries_it(monkeypatch, branch):
    """``endCallPhrases`` items are 2-140 characters and ``endCallMessage`` is
    capped at 1000. The closing line is bilingual and the phrase list is generated,
    so neither length is obvious by inspection."""
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk")
    if branch == "dashboard":
        monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    else:
        monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    built = vapi_client.build_assistant(vapi_client.vapi_config(), case_id="C-1", client_name="Aditya")
    body = built.get("assistantOverrides") or built["assistant"]

    assert body["endCallPhrases"], "the hang-up guarantee needs at least one phrase"
    assert all(2 <= len(phrase) <= 140 for phrase in body["endCallPhrases"])
    assert len(body["endCallMessage"]) <= 1000


def test_the_prompt_the_override_carries_is_within_the_field_s_limit():
    """``OpenAIMessage.content`` is capped at 100,000,000 characters — no risk in
    practice, but the prompt is assembled from several constants and this is the one
    place that records the cap exists."""
    prompt = vapi_client.model_settings(client_name="Aditya", amount=199.0)["messages"][0]["content"]
    assert 0 < len(prompt) <= 100_000_000


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
    monkeypatch.setattr(dashboard, "start_web_call", lambda *args, **kwargs: {"call": {"id": 1}, "mode": "web", "web": {"public_key": "pk"}})
    response = client.post("/api/voice/start-call", json={"case_id": "C-1"})
    assert response.status_code == 200
    assert response.get_json()["call"]["id"] == 1


def test_starting_a_call_requires_a_case_id(client):
    assert client.post("/api/voice/start-call", json={}).status_code == 400


def test_completing_a_call_requires_a_call_id(client):
    assert client.post("/api/voice/complete-call", json={}).status_code == 400
