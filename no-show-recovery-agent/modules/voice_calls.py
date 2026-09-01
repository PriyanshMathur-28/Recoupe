"""Voice recovery: the ``call_log`` store, outcome classification, and metrics.

Design contract (mirrors the metric-cards spec)
-----------------------------------------------
* **One row per call ATTEMPT, never per case.** A case with three attempts has
  three ``call_log`` rows. Nothing in this module aggregates by case except the
  attribution helper, which deliberately takes only the newest attempt.
* **No stored counters.** Every one of the five dashboard cards is a live query
  over this table (plus the recovery store). There is no incrementing column
  anywhere, so a card can never drift from the rows it describes.
* **One cycle window, applied everywhere.** :func:`start_of_current_cycle`
  returns the single boundary used by *all* cycle-scoped cards. Cards 2, 3, 4
  and 5 share it; none of them defines its own window.
* **Outcome is a closed 4-way enum.** ``promised_to_pay | declined | no_answer
  | escalated``. "answered" is *not* an outcome — it is an intermediate
  yes/no fact that decides whether classification runs at all. A browser web
  call and an outbound phone call converge on the exact same two steps:

      step 1  answered?  -> no  => outcome = "no_answer", classification skipped
                         -> yes => step 2
      step 2  the captured speech goes through the SAME typed-JSON 4-way
              classification, which may only return an ANSWERED outcome.

* **The client's final answer is its own typed question.** The 4-way outcome
  says what bucket the call falls in; it does not say what the client actually
  settled on. :func:`extract_final_answer` asks a second, separate typed
  question — refused / paying now / paying on a named date / needs a human —
  and captures the client's own closing words. It is persisted next to the
  outcome so the dashboard can show the answer without ever serving the
  transcript.

* **Attribution is decided once.** :func:`attribute_recovery` performs the
  "last action before payment" comparison (newest call vs. newest email send)
  and returns *both* the winning channel and the winning timestamp, so the
  payment webhook can persist them together and no later query has to guess a
  join. See :mod:`modules.razorpay_webhooks`.

An in-flight call (placed, not yet ended) is stored with an empty ``outcome``.
That is a real third state, not a fifth enum value: the call has no outcome yet.
Card 3 ("calls placed") counts it, because the attempt genuinely happened. Card
4 ("answer rate") excludes it from *both* numerator and denominator, because an
unfinished call is not evidence either way — including it would silently drag
the rate toward zero while calls are still ringing.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .audit_log import AUDIT_PATH, log_event

ROOT = Path(__file__).resolve().parents[1]
VOICE_DB_PATH = ROOT / "data" / "voice_calls.sqlite3"

# The closed outcome enum. Nothing outside this tuple may ever be stored.
OUTCOMES = ("promised_to_pay", "declined", "no_answer", "escalated")

# How the attempt was made. This is transport, not outcome: no card branches on
# it, so both transports are counted by identical queries. There is no simulated
# mode — every row here describes a call a provider actually carried.
#   web   - Vapi web call in the operator's browser (the primary flow)
#   live  - outbound telephony call placed through Vapi
CALL_MODES = ("web", "live")

# Outcomes reachable only when the call was actually answered. The classifier is
# restricted to these, which is what stops "answered" leaking in as a value.
ANSWERED_OUTCOMES = ("promised_to_pay", "declined", "escalated")

# Vapi `endedReason` values that mean nobody picked up. Anything else that
# reached the transcript stage is treated as answered.
UNANSWERED_REASONS = {
    "customer-did-not-answer",
    "customer-busy",
    "customer-did-not-give-microphone-permission",
    "voicemail",
    "no-answer",
    "busy",
    "failed",
    "twilio-failed-to-connect-call",
    "phone-call-provider-closed-websocket",
    "pipeline-error-no-answer",
}

CALL_FIELDS = (
    "case_id",
    "case_key",
    "placed_at",
    "ended_at",
    "outcome",
    "promise_date",
    "transcript_summary",
    "provider",
    "provider_call_id",
    "mode",
    "client_name",
    "phone",
    "answered",
    "ended_reason",
    # The client's final answer — a second typed question, stored beside the
    # outcome. Appending here is all that is needed: _connect() widens an
    # existing call_log to match this tuple on the next open.
    "final_answer_kind",
    "final_answer",
    "final_pay_date",
    "client_final_words",
)


class VoiceOutcomeError(ValueError):
    """Raised when a classification result violates the typed contract."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _connect(path: Path = VOICE_DB_PATH) -> sqlite3.Connection:
    """Open the call store, creating or widening the schema on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS call_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  case_id TEXT NOT NULL,"
        "  case_key TEXT NOT NULL DEFAULT '',"
        "  placed_at TEXT NOT NULL,"
        "  ended_at TEXT NOT NULL DEFAULT '',"
        "  outcome TEXT NOT NULL DEFAULT '',"
        "  promise_date TEXT NOT NULL DEFAULT '',"
        "  transcript_summary TEXT NOT NULL DEFAULT '',"
        "  provider TEXT NOT NULL DEFAULT 'vapi',"
        "  provider_call_id TEXT NOT NULL DEFAULT '',"
        "  mode TEXT NOT NULL DEFAULT 'live',"
        "  client_name TEXT NOT NULL DEFAULT '',"
        "  phone TEXT NOT NULL DEFAULT '',"
        "  answered TEXT NOT NULL DEFAULT '',"
        "  ended_reason TEXT NOT NULL DEFAULT '',"
        "  final_answer_kind TEXT NOT NULL DEFAULT '',"
        "  final_answer TEXT NOT NULL DEFAULT '',"
        "  final_pay_date TEXT NOT NULL DEFAULT '',"
        "  client_final_words TEXT NOT NULL DEFAULT ''"
        ")"
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(call_log)")}
    for field in CALL_FIELDS:
        if field not in existing:
            connection.execute(f"ALTER TABLE call_log ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    connection.commit()
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_call(row: sqlite3.Row) -> dict[str, Any]:
    call = {key: row[key] for key in row.keys()}
    call["id"] = int(call["id"])
    call["answered"] = None if call.get("answered") in ("", None) else call["answered"] == "true"
    for key in ("ended_at", "outcome", "promise_date", "transcript_summary", "provider_call_id", "ended_reason"):
        call[key] = call.get(key) or None
    # The final-answer columns stay as strings rather than None: the dashboard
    # renders them directly into a cell, and "" is the honest value for a call
    # that ended before the client said anything.
    for key in ("final_answer_kind", "final_answer", "final_pay_date", "client_final_words"):
        call[key] = call.get(key) or ""
    return call


def open_call(
    case_id: str,
    *,
    case_key: str = "",
    client_name: str = "",
    phone: str = "",
    provider: str = "vapi",
    provider_call_id: str = "",
    mode: str = "live",
    path: Path = VOICE_DB_PATH,
) -> dict[str, Any]:
    """Record one call ATTEMPT at the moment it is placed.

    ``placed_at`` is stamped here and never rewritten. This is the row Card 3
    counts, and its ``placed_at`` is the value attribution later compares
    against the newest email send.
    """
    if not str(case_id or "").strip():
        raise ValueError("A case_id is required to place a call")
    if mode not in CALL_MODES:
        raise ValueError(f"Call mode must be one of {CALL_MODES}")
    with _connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO call_log (case_id, case_key, placed_at, provider, provider_call_id, mode, client_name, phone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(case_id), str(case_key or ""), _now(), str(provider), str(provider_call_id or ""), mode, str(client_name or ""), str(phone or "")),
        )
        call_id = int(cursor.lastrowid or 0)
        row = connection.execute("SELECT * FROM call_log WHERE id = ?", (call_id,)).fetchone()
    return _row_to_call(row)


def close_call(
    call_id: int,
    *,
    outcome: str,
    answered: bool,
    promise_date: str | None = None,
    transcript_summary: str = "",
    ended_reason: str = "",
    final_answer: dict[str, Any] | None = None,
    path: Path = VOICE_DB_PATH,
) -> dict[str, Any]:
    """Write the terminal facts of one attempt in a single atomic UPDATE.

    ``outcome``, ``answered``, ``ended_at`` and ``promise_date`` always land
    together. A partially closed row (ended but unclassified, or classified but
    not ended) is therefore unreachable, so no card has to defend against one.

    ``final_answer`` — the payload from :func:`extract_final_answer` — lands in
    the same write for the same reason: a row showing an outcome but no final
    answer would read as "the client said nothing", which is a different claim
    from "we have not classified this yet". An unanswered call carries no final
    answer at all, because there was no client on the line to have one.
    """
    if outcome not in OUTCOMES:
        raise VoiceOutcomeError(f"outcome '{outcome}' is not one of {OUTCOMES}")
    if answered and outcome == "no_answer":
        raise VoiceOutcomeError("an answered call cannot have outcome 'no_answer'")
    if not answered and outcome != "no_answer":
        raise VoiceOutcomeError("an unanswered call must have outcome 'no_answer'")
    promise = str(promise_date or "") if outcome == "promised_to_pay" else ""
    final = dict(final_answer or {}) if answered else {}
    kind = str(final.get("kind") or "")
    if kind and kind not in FINAL_ANSWER_KINDS:
        raise VoiceOutcomeError(f"final answer kind '{kind}' is not one of {FINAL_ANSWER_KINDS}")
    with _connect(path) as connection:
        cursor = connection.execute(
            "UPDATE call_log SET ended_at = ?, outcome = ?, answered = ?, promise_date = ?, "
            "transcript_summary = ?, ended_reason = ?, final_answer_kind = ?, final_answer = ?, "
            "final_pay_date = ?, client_final_words = ? WHERE id = ? AND ended_at = ''",
            (
                _now(),
                outcome,
                "true" if answered else "false",
                promise,
                str(transcript_summary or ""),
                str(ended_reason or ""),
                kind,
                str(final.get("answer") or ""),
                str(final.get("pay_date") or ""),
                str(final.get("client_words") or ""),
                int(call_id),
            ),
        )
        if cursor.rowcount != 1:
            row = connection.execute("SELECT * FROM call_log WHERE id = ?", (int(call_id),)).fetchone()
            if row is None:
                raise LookupError(f"No call_log row with id {call_id}")
            raise ValueError(f"Call {call_id} is already closed")
        row = connection.execute("SELECT * FROM call_log WHERE id = ?", (int(call_id),)).fetchone()
    return _row_to_call(row)


def get_call(call_id: int, path: Path = VOICE_DB_PATH) -> dict[str, Any] | None:
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM call_log WHERE id = ?", (int(call_id),)).fetchone()
    return _row_to_call(row) if row else None


def find_call_by_provider_id(provider_call_id: str, path: Path = VOICE_DB_PATH) -> dict[str, Any] | None:
    """Resolve the provider's own call id back to our row, for webhook handling."""
    if not str(provider_call_id or "").strip():
        return None
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM call_log WHERE provider_call_id = ? ORDER BY id DESC LIMIT 1",
            (str(provider_call_id),),
        ).fetchone()
    return _row_to_call(row) if row else None


def attach_provider_call_id(call_id: int, provider_call_id: str, path: Path = VOICE_DB_PATH) -> None:
    """Bind the provider's call id to an already-open row."""
    with _connect(path) as connection:
        connection.execute(
            "UPDATE call_log SET provider_call_id = ? WHERE id = ?",
            (str(provider_call_id or ""), int(call_id)),
        )


def list_calls(path: Path = VOICE_DB_PATH, case_id: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
    """Return call attempts, newest first, optionally scoped to a case/window."""
    query = "SELECT * FROM call_log"
    clauses: list[str] = []
    params: list[Any] = []
    if case_id:
        clauses.append("case_id = ?")
        params.append(str(case_id))
    if since:
        clauses.append("placed_at >= ?")
        params.append(str(since))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY placed_at DESC, id DESC"
    with _connect(path) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return [_row_to_call(row) for row in rows]


def _link_sends_by_call(audit_path: Path = AUDIT_PATH) -> dict[int, str]:
    """Map ``call_log.id`` → the timestamp its follow-up email actually left.

    Whether a call caused an email is not a column on the call: the send happens
    after the row closes, as its own audited action. Rather than denormalise a
    fact the audit trail already owns, this reads it back out — the audit log is
    the store of record for "did we email them", and a column duplicating it
    could drift from it.

    Only rows whose outcome is :data:`VOICE_LINK_OUTCOME` count. A failed send is
    audited too, with ``technical_error``, and must not read as sent.
    """
    from .audit_log import read_events

    sends: dict[int, str] = {}
    try:
        rows = read_events(audit_path)
    except Exception:  # noqa: BLE001 - a missing audit store means nothing was sent yet
        return sends
    for row in rows:
        if str(row.get("action") or "") != VOICE_LINK_ACTION:
            continue
        if str(row.get("outcome") or "") != VOICE_LINK_OUTCOME:
            continue
        try:
            payload = json.loads(row.get("event_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        raw_id = payload.get("call_id") if isinstance(payload, dict) else None
        if raw_id in (None, ""):
            continue
        try:
            call_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        stamp = str(row.get("timestamp") or "")
        # Newest send wins, so a re-sent link shows its most recent timestamp.
        if stamp >= sends.get(call_id, ""):
            sends[call_id] = stamp
    return sends


def call_history(
    case_id: str,
    path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
) -> list[dict[str, Any]]:
    """Every call attempt for one case, newest first, with its email outcome.

    This is what the per-client history dropdown renders: the attempt, how it was
    classified, and whether the promised payment link actually went out. The two
    facts live in two stores by design — the attempt in ``call_log``, the send in
    the audit trail — and are joined here on ``call_id`` so the UI never has to
    know that.
    """
    sends = _link_sends_by_call(audit_path)
    history: list[dict[str, Any]] = []
    for call in list_calls(path, case_id=str(case_id)):
        sent_at = sends.get(int(call["id"]), "")
        history.append({**call, "email_sent": bool(sent_at), "email_sent_at": sent_at})
    return history


def latest_call_placed_at(case_id: str, path: Path = VOICE_DB_PATH) -> str | None:
    """MAX(call_log.placed_at) for one case — the call side of attribution."""
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT MAX(placed_at) AS latest FROM call_log WHERE case_id = ?",
            (str(case_id),),
        ).fetchone()
    return (row["latest"] or None) if row else None


# ---------------------------------------------------------------------------
# Cycle window — one definition, shared by every cycle-scoped card
# ---------------------------------------------------------------------------

# Audit outcomes that mean an email actually left the building. Used for the
# email side of the attribution comparison.
EMAIL_SENT_OUTCOMES = {"invoice_sent", "sent_without_link"}

# The audit action and outcome written when a *call* causes a payment link to be
# emailed. Both names are deliberately outside the sets other layers key on:
#
# * ``VOICE_LINK_OUTCOME`` is not in :data:`EMAIL_SENT_OUTCOMES`, so
#   :func:`latest_email_sent_at` cannot see it. That is the whole point — an
#   email the agent sent *because of* the call is a consequence of the call, not
#   a competing channel, and letting it register would flip
#   :func:`attribute_recovery` from "call" to "email" every single time and
#   permanently zero the "Recovered via Voice" card.
# * ``VOICE_LINK_ACTION`` is not in ``service_layer.CASE_ACTIONS``, so writing it
#   cannot rewrite the case's current condition. An escalated case that received
#   a voice-triggered link is still an escalated case.
VOICE_LINK_ACTION = "voice_payment_link_sent"
VOICE_LINK_OUTCOME = "voice_promise_link_sent"


def start_of_current_cycle(audit_path: Path = AUDIT_PATH) -> str | None:
    """Return the ISO timestamp the current recovery cycle began, or None.

    The audit log is rebuilt from scratch every time an operator uploads a
    recovery CSV (see ``/api/upload-csv`` → ``run_batch(reset_audit=True)``), so
    the oldest audit row *is* the start of the cycle currently on screen. Using
    one derived boundary rather than four hand-written ones is what keeps
    "Calls placed: 12 this run" from sitting next to an all-time promise count.

    Returns None when no cycle has started, in which case callers must treat the
    window as "everything" — there is nothing older to exclude.
    """
    if not audit_path.exists():
        return None
    try:
        with audit_path.open(newline="", encoding="utf-8") as handle:
            stamps = [str(row.get("timestamp") or "") for row in csv.DictReader(handle)]
    except OSError:
        return None
    stamps = [stamp for stamp in stamps if stamp]
    return min(stamps) if stamps else None


def latest_email_sent_at(case_id: str, audit_path: Path = AUDIT_PATH, attempts_path: Path | None = None) -> str | None:
    """Newest confirmed email-send timestamp for a case — the email side of attribution.

    Two sources are consulted and the newer wins: the confirmed-send store
    (``client_email_status.last_email_sent_at``, written only after the delivery
    provider accepted the message) and the audit log's send rows. The store is
    authoritative for "did it actually go out"; the audit log covers sends made
    before that store existed.
    """
    candidates: list[str] = []
    try:
        from .attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH, get_client_email_status

        status = get_client_email_status(str(case_id), attempts_path or ATTEMPTS_DB_PATH)
        if status and status.get("last_email_sent_at"):
            candidates.append(str(status["last_email_sent_at"]))
    except Exception:  # noqa: BLE001 - a missing attempts DB must not break attribution
        pass
    if audit_path.exists():
        try:
            with audit_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("client_id") or "") != str(case_id):
                        continue
                    if str(row.get("outcome") or "") in EMAIL_SENT_OUTCOMES:
                        candidates.append(str(row.get("timestamp") or ""))
        except OSError:
            pass
    candidates = [stamp for stamp in candidates if stamp]
    return max(candidates) if candidates else None


def attribute_recovery(
    case_id: str,
    *,
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    voice_path: Path = VOICE_DB_PATH,
) -> tuple[str | None, str | None]:
    """Decide which channel gets credit for a payment, and from which instant.

    This is the "last action before payment" rule, written down once and
    performed once, at webhook time:

        newest ``call_log.placed_at``  vs.  newest confirmed email-send timestamp
        whichever is more recent wins

    Returns ``(recovered_via, recovery_triggered_at)``. The second value is the
    winning timestamp itself, which the caller persists as
    ``recovery_triggered_at`` in the same atomic write as ``recovered_via``.
    That is what lets "Avg time to payment" be a plain subtraction later instead
    of an unspecified join back into ``call_log``.

    ``(None, None)`` means neither channel ever acted on this case, so no
    attribution is honest — the recovery is recorded without a channel rather
    than defaulting to one.
    """
    call_at = latest_call_placed_at(str(case_id), voice_path)
    email_at = latest_email_sent_at(str(case_id), audit_path, attempts_path)
    if call_at and email_at:
        return ("call", call_at) if call_at >= email_at else ("email", email_at)
    if call_at:
        return "call", call_at
    if email_at:
        return "email", email_at
    return None, None


# ---------------------------------------------------------------------------
# Outcome classification (step 2 — only ever runs on an ANSWERED call)
# ---------------------------------------------------------------------------

CLASSIFIER_PROMPT = """You classify the reply a client gave on a debt-recovery phone call.

You have NO execution authority. You never promise anything, never quote an
amount, never create a payment link. You only label what the client said.

Reply with ONE JSON object and no other text, no markdown fence, no commentary:
{
  "outcome": one of %(outcomes)s,
  "promise_date": an ISO date "YYYY-MM-DD" when outcome is "promised_to_pay", otherwise null,
  "summary": one neutral sentence under 200 characters describing what the client said,
  "confidence": number between 0.0 and 1.0
}

How to choose the outcome:
- "promised_to_pay": the client committed to paying, with or without a named date.
- "declined": the client refused, disputed the amount, or said they will not pay.
- "escalated": the reply needs a human — a complaint, a legal threat, a claim of
  fraud, a request to speak to someone, or anything you cannot confidently label.

Never output "no_answer". You are only ever given calls that were answered.
Never invent a promise. If no commitment was actually made, it is not a promise.
""" % {"outcomes": json.dumps(list(ANSWERED_OUTCOMES))}

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PROMISE_HINTS = ("i will pay", "i'll pay", "will pay", "pay it", "send the money", "transfer", "by tomorrow", "by monday", "next week", "paying today", "promise", "sure, ", "okay i", "yes i will")
_DECLINE_HINTS = ("not paying", "won't pay", "will not pay", "refuse", "cancel", "dispute", "already paid", "not my", "wrong number", "no thanks", "not interested")
_ESCALATE_HINTS = ("lawyer", "legal", "court", "complaint", "manager", "supervisor", "fraud", "harass", "police", "consumer")


def heuristic_outcome(transcript: str) -> dict[str, Any]:
    """Deterministic 4-way fallback so the pipeline never needs a live model.

    Returns the same shape the model must return, restricted to ANSWERED
    outcomes. An unrecognisable reply becomes ``escalated`` with low confidence,
    never a promise — guessing a promise would create a payment expectation the
    client never made.
    """
    text = str(transcript or "").strip().lower()
    if not text:
        return {"outcome": "escalated", "promise_date": None, "summary": "The call was answered but no speech was captured.", "confidence": 0.4, "source": "heuristic"}
    if any(hint in text for hint in _ESCALATE_HINTS):
        return {"outcome": "escalated", "promise_date": None, "summary": "The client raised a complaint or legal concern that needs a person.", "confidence": 0.8, "source": "heuristic"}
    if any(hint in text for hint in _DECLINE_HINTS):
        return {"outcome": "declined", "promise_date": None, "summary": "The client declined to pay or disputed the charge.", "confidence": 0.78, "source": "heuristic"}
    if any(hint in text for hint in _PROMISE_HINTS):
        return {"outcome": "promised_to_pay", "promise_date": None, "summary": "The client committed to paying.", "confidence": 0.76, "source": "heuristic"}
    return {"outcome": "escalated", "promise_date": None, "summary": "The reply could not be confidently classified.", "confidence": 0.4, "source": "heuristic"}


def validate_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce model output to the typed contract; reject anything outside it."""
    if not isinstance(payload, dict):
        raise VoiceOutcomeError("classification must be an object")
    outcome = str(payload.get("outcome") or "").strip().lower()
    if outcome not in ANSWERED_OUTCOMES:
        raise VoiceOutcomeError(f"outcome '{outcome}' is not one of {ANSWERED_OUTCOMES}")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        confidence = 0.5
    promise_raw = payload.get("promise_date")
    promise = str(promise_raw).strip() if promise_raw not in (None, "", "null") else ""
    if outcome != "promised_to_pay":
        promise = ""
    elif promise and not _ISO_DATE.match(promise):
        # A malformed date is dropped, not guessed. The promise still stands;
        # only the date is unknown.
        promise = ""
    summary = str(payload.get("summary") or "").strip()[:200]
    return {
        "outcome": outcome,
        "promise_date": promise or None,
        "summary": summary,
        "confidence": round(confidence, 2),
        "source": "llm",
    }


def _extract_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    match = _JSON_OBJECT.search(text)
    if not match:
        raise VoiceOutcomeError("no JSON object in model output")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VoiceOutcomeError(f"model output is not valid JSON: {exc}") from exc
    return parsed


def _call_llm(transcript: str, prompt: str = CLASSIFIER_PROMPT) -> str:
    """Ask the configured provider for a typed answer. Groq, then Gemini.

    ``prompt`` is a parameter rather than a constant because two different typed
    questions are asked of the same providers with the same fallback order: the
    4-way outcome classification, and the follow-up-email decision. Sharing this
    function means a provider outage degrades both in the same way.
    """
    import requests

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Call transcript:\n{transcript}"},
    ]
    groq_key = os.getenv("GROQ_API_KEY")
    errors: list[str] = []
    if groq_key:
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}"},
                json={"model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"), "messages": messages, "temperature": 0.1},
                timeout=30,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - provider failure falls back
            errors.append(f"Groq: {exc}")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}",
                json={"contents": [{"parts": [{"text": f"{prompt}\n\nCall transcript:\n{transcript}"}]}], "generationConfig": {"temperature": 0.1}},
                timeout=30,
            )
            response.raise_for_status()
            return str(response.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Gemini: {exc}")
    raise RuntimeError("; ".join(errors) or "no LLM provider configured")


def classify_reply(transcript: str, caller: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Step 2 of the two-step outcome rule: label an ANSWERED call's reply.

    Identical for a web call and a phone call — that sameness is the point. Any
    model failure degrades to :func:`heuristic_outcome`, so the
    dashboard's four-way split is always populated.
    """
    invoke = caller or _call_llm
    try:
        return validate_outcome(_extract_json(invoke(transcript)))
    except (VoiceOutcomeError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
        return heuristic_outcome(transcript)


def resolve_call_outcome(
    *,
    answered: bool,
    transcript: str = "",
    ended_reason: str = "",
    caller: Callable[[str], str] | None = None,
    final_answer_caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run the full two-step rule and return a closable outcome payload.

    Step 1 (``answered``) is decided by the caller — the silence window for a
    browser web call, or Vapi's ``endedReason`` on an outbound phone call. Step 2
    only happens when step 1 said yes.

    An answered call also carries ``final_answer``: what the client actually
    settled on, in their own words. It rides along in this payload rather than
    being fetched separately by each closing path, so the browser path, the
    end-of-call webhook and the assistant's own tool report all record it.
    """
    if not answered:
        return {
            "outcome": "no_answer",
            "promise_date": None,
            "summary": "Nobody picked up.",
            "confidence": 1.0,
            "source": "silence_window" if not ended_reason else ended_reason,
            "answered": False,
            # Nobody spoke, so there is no final answer to report. This is not a
            # missing value the UI should fill in with a guess.
            "final_answer": None,
        }
    classified = classify_reply(transcript, caller)
    return {
        **classified,
        "answered": True,
        "final_answer": extract_final_answer(transcript, classified, final_answer_caller),
    }


def answered_from_ended_reason(ended_reason: str, transcript: str = "") -> bool:
    """Map a provider hangup reason to step 1's yes/no, transcript as tiebreak.

    A transcript on which only the agent spoke is not a tiebreak in favour of
    "answered". The agent always speaks first, so its greeting is present on
    every call including the ones nobody was ever on.
    """
    reason = str(ended_reason or "").strip().lower()
    if reason in UNANSWERED_REASONS:
        return False
    spoken = str(transcript or "").strip()
    if not spoken or agent_only_transcript(spoken):
        return False
    return True


# ---------------------------------------------------------------------------
# The client's final answer — a separate typed question from the outcome
# ---------------------------------------------------------------------------
#
# The 4-way outcome answers "which bucket does this call belong in". It does not
# answer "what did the client actually settle on", and the two genuinely differ:
# a client who says "some other day" and a client who says "I'll pay right now"
# are both promised_to_pay, yet an operator needs to treat them differently.
#
# So this is asked as its own typed question rather than by widening OUTCOMES.
# Widening the enum would break every card that branches on it and force the
# classifier to choose between a bucket and a nuance. A separate question keeps
# the enum closed and the nuance precise.
FINAL_ANSWER_KINDS = (
    "paying_now",       # paying immediately / already paying while on the call
    "paying_on_date",   # committed, with or without a named day
    "refused",          # will not pay, disputes it, or claims it is already paid
    "needs_human",      # complaint, legal threat, wants a person
    "unclear",          # answered, but stated no position we can act on
)

FINAL_ANSWER_PROMPT = """You report the client's FINAL position at the end of a debt-recovery call.

You have NO execution authority. You do not decide the call's outcome, quote an
amount, create a link, or send anything. You only report what the client settled
on by the end of the conversation.

Reply with ONE JSON object and no other text, no markdown fence, no commentary:
{
  "kind": one of %(kinds)s,
  "answer": one short operator-facing sentence under 140 characters stating the client's final position,
  "pay_date": an ISO date "YYYY-MM-DD" only if the client named or clearly implied a specific day, otherwise null,
  "client_words": the client's own final words, quoted from the transcript, under 160 characters, in the language they spoke,
  "confidence": number between 0.0 and 1.0
}

How to choose the kind:
- "paying_now": the client said they are paying immediately, today, or right now.
- "paying_on_date": the client committed to paying later — whether they named the
  day or only said "another day", "next week", "after my salary".
- "refused": the client will not pay, disputes the charge, says it was already
  paid, or says it is not theirs.
- "needs_human": a complaint, a legal or fraud claim, or a request for a person.
- "unclear": the client spoke but committed to nothing you can act on.

Rules that override everything above:
- The LAST thing the client said wins. If they first said "today" and later said
  "some other day", the final answer is "some other day".
- Only set "pay_date" for a day the client actually indicated. Never fill it in
  from today's date to make the record look complete. "Another day" with no day
  named is "paying_on_date" with a null pay_date.
- "client_words" is quoted, never translated and never paraphrased. If the client
  said nothing quotable, use an empty string.
""" % {"kinds": json.dumps(list(FINAL_ANSWER_KINDS))}

# Deterministic hints for the fallback, in both English and Hindi, because the
# assistant speaks whichever language the client does and the fallback must not
# be blind in one of them.
_FINAL_REFUSE_HINTS = (
    "not paying", "won't pay", "will not pay", "refuse", "dispute", "already paid",
    "not my", "wrong number", "no thanks", "not interested", "cancel",
    "नहीं करूंगा", "नहीं करूँगा", "नहीं दूंगा", "नहीं दूँगा", "पैसे नहीं",
    "पहले ही", "कर दिया है", "गलत नंबर", "ग़लत नंबर", "मेरा नहीं",
)
_FINAL_HUMAN_HINTS = (
    "lawyer", "legal", "court", "complaint", "manager", "supervisor", "fraud",
    "harass", "police", "consumer",
    "वकील", "क़ानूनी", "कानूनी", "अदालत", "शिकायत", "मैनेजर", "पुलिस", "धोखा",
)
_FINAL_LATER_HINTS = (
    "another day", "other day", "some other day", "next week", "next month",
    "tomorrow", "day after", "later", "after salary", "by monday", "by friday",
    "और दिन", "दूसरे दिन", "दुसरे दिन", "कल", "परसों", "बाद में", "अगले हफ्ते",
    "अगले सप्ताह", "सैलरी", "तनख्वाह",
)
_FINAL_NOW_HINTS = (
    "right now", "paying now", "pay now", "today", "immediately", "just now",
    "doing it now", "i'll do it now", "straight away",
    "अभी", "आज", "तुरंत", "कर देता हूं", "कर देता हूँ", "कर रहा हूं", "कर रहा हूँ",
)

_RELATIVE_DAYS = {
    "today": 0, "आज": 0,
    "tomorrow": 1, "कल": 1,
    "day after": 2, "परसों": 2,
}


# Speaker prefixes, kept next to the transcript readers that depend on them. The
# browser writes "Agent: "/"Client: " and Vapi's own transcripts arrive as
# role-labelled turns normalised to the same shape, so both sides of a call are
# identifiable — but only when a prefix is actually present.
_AGENT_PREFIXES = ("agent:", "assistant:", "ai:", "bot:")
_CLIENT_PREFIXES = ("client:", "customer:", "user:", "caller:")


def _client_lines(transcript: str) -> list[str]:
    """The client's turns only, in order, stripped of the speaker prefix.

    The final answer is a fact about what the CLIENT said. Reading the agent's
    turns into it is how a summary ends up asserting a promise the agent
    proposed and the client never accepted.
    """
    lines: list[str] = []
    for raw in str(transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        for prefix in _CLIENT_PREFIXES:
            if lowered.startswith(prefix):
                spoken = line[len(prefix):].strip()
                if spoken:
                    lines.append(spoken)
                break
    return lines


def agent_only_transcript(transcript: str) -> bool:
    """Did the agent speak every turn on this transcript?

    Step 1 asks whether a human engaged, and a transcript is only evidence of
    that when some of it is theirs. A call cut off during the agent's greeting
    still produces two agent lines, and counting those as a conversation is what
    filed an empty call as answered — with a 100% answer rate and an
    ``escalated`` outcome describing a client who never spoke.

    Returns ``False`` when no speaker is identifiable, which is deliberate: an
    unprefixed transcript is somebody's speech and this cannot say whose, so the
    transcript keeps its power as evidence and only a provably one-sided call
    loses it.
    """
    agent_spoke = False
    for raw in str(transcript or "").splitlines():
        lowered = raw.strip().lower()
        if not lowered:
            continue
        if lowered.startswith(_CLIENT_PREFIXES):
            return False
        if lowered.startswith(_AGENT_PREFIXES):
            agent_spoke = True
        else:
            # An unattributed line. Whose it is cannot be established, so this
            # is not a one-sided transcript.
            return False
    return agent_spoke


def _resolve_relative_day(text: str) -> str:
    """Turn 'today'/'आज'/'tomorrow'/'कल' into an ISO date. Nothing else."""
    lowered = str(text or "").lower()
    for phrase, offset in _RELATIVE_DAYS.items():
        if phrase in lowered:
            return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()
    return ""


def heuristic_final_answer(transcript: str, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic final-answer fallback, weighted to the client's LAST turn.

    Mirrors :func:`heuristic_outcome`'s contract: same shape as the model must
    return, and never more confident than the evidence. With no client speech at
    all this reports ``unclear`` rather than borrowing the outcome's optimism —
    an empty transcript is not a commitment.
    """
    lines = _client_lines(transcript)
    tail = " ".join(lines[-2:]).strip()
    last = lines[-1] if lines else ""
    haystack = tail.lower() or str(transcript or "").lower()

    def answer(kind: str, sentence: str, confidence: float, pay_date: str = "") -> dict[str, Any]:
        return {
            "kind": kind,
            "answer": sentence,
            "pay_date": pay_date or None,
            "client_words": last[:160],
            "confidence": confidence,
            "source": "heuristic",
        }

    if not lines:
        return answer("unclear", "The client's own words were not captured on this call.", 0.3)
    # "I have no money" reads as a refusal word-for-word and is not one: the
    # client is unable to pay in full, which is the opening for a plan rather
    # than a refusal to engage. Where the same sentence trips both lists the
    # inability wins, and it is reported as unsettled rather than refused —
    # whether a plan was actually requested is :func:`detect_plan_request`'s
    # question, not this one's.
    if any(hint in haystack for hint in _FINAL_REFUSE_HINTS):
        if any(hint in haystack for hint in _PLAN_REQUEST_HINTS):
            return answer(
                "unclear",
                "The client said they cannot pay the full amount right now.",
                0.5,
            )
        return answer("refused", "The client refused to pay or disputed the amount.", 0.75)
    if any(hint in haystack for hint in _FINAL_HUMAN_HINTS):
        return answer("needs_human", "The client raised something that needs a person, not a link.", 0.75)
    # "Later" is tested before "now" on purpose: a client who says "not today,
    # some other day" contains both, and the later commitment is the real answer.
    if any(hint in haystack for hint in _FINAL_LATER_HINTS):
        return answer("paying_on_date", "The client will pay on another day.", 0.7, _resolve_relative_day(haystack) if "कल" in haystack or "tomorrow" in haystack or "परसों" in haystack or "day after" in haystack else "")
    if any(hint in haystack for hint in _FINAL_NOW_HINTS):
        return answer("paying_now", "The client said they are paying immediately.", 0.7, _resolve_relative_day(haystack))
    promise = str((classification or {}).get("promise_date") or "")
    if str((classification or {}).get("outcome") or "") == "promised_to_pay":
        return answer("paying_on_date", "The client agreed to pay; no day was clearly stated.", 0.45, promise)
    return answer("unclear", "The client spoke but settled on nothing actionable.", 0.35)


FINAL_ANSWER_FALLBACK_TEXT = {
    "paying_now": "The client said they are paying immediately.",
    "paying_on_date": "The client committed to paying on a later day.",
    "refused": "The client refused to pay.",
    "needs_human": "The client needs a person to handle this.",
    "unclear": "The client settled on nothing actionable.",
}


def validate_final_answer(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's final-answer report to the typed contract."""
    if not isinstance(payload, dict):
        raise VoiceOutcomeError("final answer must be an object")
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in FINAL_ANSWER_KINDS:
        raise VoiceOutcomeError(f"kind '{kind}' is not one of {FINAL_ANSWER_KINDS}")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        confidence = 0.5
    raw_date = payload.get("pay_date")
    pay_date = str(raw_date).strip() if raw_date not in (None, "", "null") else ""
    # A malformed or invented date is dropped, never repaired. "They will pay"
    # with an unknown day is true; "they will pay on <a date we made up>" is not.
    if pay_date and not _ISO_DATE.match(pay_date):
        pay_date = ""
    if kind in ("refused", "needs_human", "unclear"):
        pay_date = ""
    answer = str(payload.get("answer") or "").strip()[:140]
    words = str(payload.get("client_words") or "").strip()[:160]
    return {
        "kind": kind,
        "answer": answer or FINAL_ANSWER_FALLBACK_TEXT[kind],
        "pay_date": pay_date or None,
        "client_words": words,
        "confidence": round(confidence, 2),
        "source": "llm",
    }


def extract_final_answer(
    transcript: str,
    classification: dict[str, Any] | None = None,
    caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Ask a model what the client FINALLY said, and quote them saying it.

    A third typed question over the same provider chain as the outcome
    classification and the email decision, so all three degrade together. It is
    reporting only: nothing here can change an outcome, cause a send, or move a
    promise date. That is why it can run on every answered call, including
    refusals, where the email decision is never consulted at all.

    Any failure degrades to :func:`heuristic_final_answer`, so the dashboard
    column is always populated.
    """
    outcome = str((classification or {}).get("outcome") or "")
    briefing = (
        f"Assigned outcome (you cannot change it): {outcome or 'unknown'}\n"
        f"Today's date: {datetime.now(timezone.utc).date().isoformat()}\n\n"
        f"{transcript}"
    )
    invoke = caller or (lambda text: _call_llm(text, FINAL_ANSWER_PROMPT))
    try:
        result = validate_final_answer(_extract_json(invoke(briefing)))
    except (VoiceOutcomeError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
        return heuristic_final_answer(transcript, classification)
    if not result["client_words"]:
        # The model may summarise instead of quoting. The client's own last turn
        # is already in the transcript, so take it from there rather than
        # letting the column claim the client said nothing.
        lines = _client_lines(transcript)
        result["client_words"] = (lines[-1] if lines else "")[:160]
    return result


# ---------------------------------------------------------------------------
# The flexible-plan request — a fourth typed question, not a fifth outcome
# ---------------------------------------------------------------------------

# A client who says they cannot pay the full amount has not refused, and has not
# promised the whole debt either: they have asked for a different SHAPE of
# payment. The 4-way outcome still describes such a call correctly, so this
# nuance is asked as its own typed question for exactly the reason the final
# answer is — widening OUTCOMES would break every card that branches on it and
# force the classifier to choose between a bucket and a nuance.
#
# The answer is reporting plus one number: whether the CLIENT asked to split the
# debt, and any figure they volunteered as a first payment. That figure is the
# whole reason the question carries a number at all — it is handed to the
# chatbot so the customer never has to repeat what they already said aloud.
#
# Nothing here approves anything. A request only diverts the follow-up from "the
# full-amount link" to "an invitation to negotiate"; the schedule that comes out
# of that negotiation is still checked by the policy layer.

PLAN_REQUEST_PROMPT = """You detect ONE thing on a debt-recovery call: did the client ask to pay in a different shape than the full amount at once?

You have NO execution authority. You do not decide the call's outcome, approve a
plan, quote a total, create a link, or send anything. You only report what the
client asked for.

Reply with ONE JSON object and no other text, no markdown fence, no commentary:
{
  "requested": true or false,
  "initial_amount": the number the client said they could pay FIRST, as a plain number with no currency symbol, or null if they named none,
  "note": one short operator-facing sentence under 140 characters describing what the client asked for,
  "client_words": the client's own words asking for it, quoted from the transcript, under 160 characters, in the language they spoke,
  "confidence": number between 0.0 and 1.0
}

Set "requested" true when the CLIENT says any of: they cannot pay the full
amount, they can only pay part of it, they want installments or an EMI, they can
pay something now and the rest later, they need to split the payment, or they ask
what payment options are available.

Also set "requested" true when the client asks whether ANY OTHER ARRANGEMENT
exists, in any wording and in any language, even without the words "payment" or
"installment" — "is there another plan?", "any other option?", "कोई और plan है
आपके पास?", "koi aur tarika hai?". A client asking what else you can do has
asked for a plan. Answering the agent's own offer of a plan with a plain "yes"
counts as well: the request is theirs the moment they accept it.

Set "requested" false when: only the AGENT mentioned a plan and the client did
not take it up; the client refused to pay at all and showed no interest in an
alternative; the client asked for more TIME on the full amount rather than a
split; or the client agreed to pay the full amount.

Rules that override everything above:
- Only the client can request a plan. The agent offering one is not a request.
- "initial_amount" is a figure the client actually said. Never split the total
  yourself, never estimate, and never copy an amount the agent named. If the
  client named no figure, it is null.
- "client_words" is quoted, never translated and never paraphrased. If the client
  said nothing quotable, use an empty string.
"""

# Deterministic hints for the fallback, in both English and Hindi, for the same
# reason the final-answer hints are bilingual: the assistant speaks whichever
# language the client does, and the fallback must not be blind in one of them.
#
# "I have no money" is included deliberately, in both languages and without a
# qualifier. It is the single most common way a client asks for a different shape
# of payment, and treating it as a flat refusal was the whole bug: the client was
# sent nothing and the call was closed on them. Inability to pay is an opening
# for a plan, not a refusal to engage.
#
# What still must NOT match is a refusal that never mentions money at all — "I am
# not paying", "this is not my bill", "I already paid". None of the phrases below
# appear in those, so the flat-refusal case stays intact.
_PLAN_REQUEST_HINTS = (
    "can't pay the full", "cannot pay the full", "can't pay full", "cannot pay full",
    "can't pay it all", "can't pay everything", "can't pay the whole",
    "can't afford", "cannot afford", "don't have enough", "do not have enough",
    "not enough money", "short of money", "short on money", "tight right now",
    "no money", "don't have money", "do not have money", "dont have money",
    "have no money", "haven't got money", "havent got money", "out of money",
    "money problem", "money is tight", "financial problem", "no funds",
    "can't pay right now", "cannot pay right now", "can't pay today",
    "installment", "installments", "instalment", "instalments", "emi",
    "in parts", "part payment", "partial payment", "split the payment", "split it",
    "some now", "some amount now", "pay some", "half now", "rest later",
    "rest on", "remaining later", "payment plan", "payment options", "flexible",
    # A client ASKING WHAT ELSE IS AVAILABLE is asking for a plan, even when
    # they never say the word "payment". The live call that exposed this had the
    # client say "कोई और plan है आपके पास?" — bare "plan", code-switched, and
    # matched by none of the phrases above, so the request was missed and the
    # customer was sent nothing. "plan" is only matched with a qualifier that
    # makes it a question about alternatives, so "cancel my plan" stays clear.
    "another plan", "other plan", "any plan", "different plan", "some other plan",
    "other option", "another option", "any option", "other way", "another way",
    "what else can", "anything else you can", "alternative",
    "कोई और plan", "और plan", "दूसरा plan", "कोई plan", "plan है",
    "कोई और तरीका", "कोई तरीका", "और कोई रास्ता", "कोई रास्ता", "कोई और option",
    "koi aur plan", "koi plan", "dusra plan", "doosra plan", "aur koi plan",
    "koi aur tarika", "koi tarika", "koi aur rasta", "koi rasta",
    "koi aur option", "koi option", "aur kya kar sakte",
    "पूरा नहीं", "पूरे पैसे नहीं", "इतने पैसे नहीं", "पैसे कम", "किस्त", "किश्त",
    "किस्तों", "किश्तों", "थोड़े पैसे", "थोड़ा अभी", "आधा अभी", "बाकी बाद",
    "बाकी में", "टुकड़ों", "ईएमआई", "एक साथ नहीं",
    "पैसे नहीं", "पैसा नहीं", "पैसे नहीं हैं", "पैसा नहीं है", "पैसे खत्म",
    "तंगी", "दिक्कत है पैसे", "अभी नहीं दे सकता", "अभी नहीं दे सकती",
    # Romanised Hinglish, because the transcriber returns whichever script it
    # heard and a client who says "paise nahi hain" asked for exactly the same
    # thing as one who says "पैसे नहीं हैं".
    "paise nahi", "paise nahin", "paisa nahi", "paisa nahin", "paise kam",
    "paise nai", "poore paise nahi", "pura nahi", "puura nahi", "ek saath nahi",
    "kist", "kisht", "kiston", "kishton", "thoda abhi", "aadha abhi",
    "baaki baad", "baki baad", "abhi nahi de sakta", "abhi nahi de sakti",
)

# ISO-looking dates are removed before scanning for money so that "pay on
# 2026-09-04" cannot be read as ₹2,026.
_DATE_LIKE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")
_SPOKEN_AMOUNT = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(k\b|thousand|हज़ार|हजार|lakh|लाख)?",
    re.IGNORECASE,
)
_THOUSANDS = {"k", "thousand", "हज़ार", "हजार"}
_LAKHS = {"lakh", "लाख"}


def _spoken_amounts(text: str) -> list[float]:
    """Every plausible money figure in an utterance, in the order it was spoken.

    A bare number below 100 with no multiplier is ignored: "the 4th" and "in 2
    weeks" are not amounts, and reading them as one would prefill the chatbot
    with a figure the client never offered.
    """
    amounts: list[float] = []
    for digits, suffix in _SPOKEN_AMOUNT.findall(_DATE_LIKE.sub(" ", str(text or ""))):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        unit = suffix.strip().lower()
        if unit in _THOUSANDS:
            value *= 1000
        elif unit in _LAKHS:
            value *= 100000
        elif value < 100:
            continue
        if value > 0:
            amounts.append(round(value, 2))
    return amounts


# The agent's own offer, as the prompt instructs it to phrase the question, plus
# the paraphrases a language model reliably produces from that instruction. Used
# only to recognise the turn a client is answering — never to count the offer
# itself as a request.
_PLAN_OFFER_HINTS = (
    "payment plan", "customised payment plan", "customized payment plan",
    "email you the link", "email you a secure link", "shall i email",
    "link to set it up", "set up a plan", "plan that works for you",
    "भुगतान योजना", "payment plan bhej", "plan bhej", "link bhej",
    "किस्तों में", "kiston mein", "link email",
)

# A client accepting that offer. Short by nature — the whole turn is often one
# word — so these are matched against the turn as a whole rather than searched
# for inside a longer sentence, which is what keeps "no, not okay" out.
_PLAN_ACCEPT_HINTS = (
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "please", "please do",
    "yes please", "go ahead", "send it", "send it please", "email it",
    "email me", "that works", "sounds good", "alright", "fine",
    "हाँ", "हां", "जी", "जी हाँ", "जी हां", "ठीक है", "ठीक", "हाँ जी",
    "भेज दो", "भेज दीजिए", "भेज देना", "कर दो", "हाँ भेज दो",
    "haan", "haa", "ha", "ji", "ji haan", "haan ji", "theek hai", "thik hai",
    "theek", "bhej do", "bhej dijiye", "bhej dena", "kar do", "haan bhej do",
)

# A refusal of the offer, tested first so "no thanks" and "nahi, mat bhejo"
# cannot be read as acceptance through the "thanks" or the "bhej".
_PLAN_DECLINE_HINTS = (
    "no", "no thanks", "no thank you", "not interested", "don't", "do not",
    "नहीं", "नहीं चाहिए", "मत", "nahi", "nahin", "nai", "mat", "nahi chahiye",
)


def _speaker_turns(transcript: str) -> list[tuple[str, str]]:
    """The transcript as ``[(speaker, words)]`` with ``speaker`` in
    ``{"agent", "client", ""}``.

    Sequence matters for one question this module asks — did the client accept
    what the agent just offered? — and that cannot be answered from the client's
    turns alone.
    """
    turns: list[tuple[str, str]] = []
    for raw in str(transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        for prefix in _AGENT_PREFIXES:
            if lowered.startswith(prefix):
                turns.append(("agent", line[len(prefix):].strip()))
                break
        else:
            for prefix in _CLIENT_PREFIXES:
                if lowered.startswith(prefix):
                    turns.append(("client", line[len(prefix):].strip()))
                    break
            else:
                turns.append(("", line))
    return [(who, words) for who, words in turns if words]


def accepted_plan_offer(transcript: str) -> str:
    """The client's words accepting an offered payment plan, or ``""``.

    The user's own instruction for the call is that the agent ASKS whether a
    customised plan is wanted, so the commonest request on a well-run call is a
    one-word "yes" that mentions neither money nor installments. Read from the
    client's turns alone that is indistinguishable from agreeing to pay in full;
    only the agent's preceding question tells them apart, which is why this
    walks the call in order.

    A later refusal wins: a client who says "yes" and then "no, don't send it"
    has declined, and the last thing they said about the offer is the answer.
    """
    accepted = ""
    offered = False
    for who, words in _speaker_turns(transcript):
        if who == "agent":
            if any(hint in words.lower() for hint in _PLAN_OFFER_HINTS):
                offered = True
            continue
        if who != "client" or not offered:
            continue
        # Punctuation and filler are stripped so a whole turn like "Haan, ok."
        # is compared as the two words it is.
        spoken = re.sub(r"[^\w\s\u0900-\u097f]+", " ", words.lower()).strip()
        collapsed = " ".join(spoken.split())
        if not collapsed:
            continue
        tokens = collapsed.split()
        if any(token in _PLAN_DECLINE_HINTS for token in tokens) or collapsed in _PLAN_DECLINE_HINTS:
            accepted = ""
            continue
        if collapsed in _PLAN_ACCEPT_HINTS or (len(tokens) <= 5 and any(token in _PLAN_ACCEPT_HINTS for token in tokens)):
            accepted = words[:160]
    return accepted


def heuristic_plan_request(transcript: str) -> dict[str, Any]:
    """Deterministic flexible-plan fallback, read from the call in order.

    Conservative on purpose, and in the opposite direction to
    :func:`decide_follow_up_email`: an unreachable model must not invent a
    request, because a request diverts the follow-up away from the payment link
    the agent promised aloud. Silence therefore means "not requested".

    Two things count as a request. The client asking for one in their own words
    is the obvious case. The client saying "yes" to the agent's offer of one is
    the other, and it is the case a well-run call produces most often — the
    agent is instructed to ask, so the client never has to find the vocabulary.
    """
    lines = _client_lines(transcript)
    matches = [index for index, line in enumerate(lines) if any(hint in line.lower() for hint in _PLAN_REQUEST_HINTS)]
    if not matches:
        if accepted := accepted_plan_offer(transcript):
            return {
                "requested": True,
                "initial_amount": None,
                "note": "The client accepted the payment plan the agent offered.",
                "client_words": accepted,
                "confidence": 0.65,
                "source": "heuristic",
            }
        return {
            "requested": False,
            "initial_amount": None,
            "note": "The client did not ask to pay in parts.",
            "client_words": "",
            "confidence": 0.4 if lines else 0.3,
            "source": "heuristic",
        }
    first = matches[0]
    # Amounts are read from the request onward: a figure the client mentioned
    # before asking to split is usually the debt being quoted back at us, not an
    # offer of a first payment.
    amounts = _spoken_amounts(" ".join(lines[first:]))
    return {
        "requested": True,
        "initial_amount": amounts[0] if amounts else None,
        "note": "The client asked to pay the amount in parts rather than all at once.",
        "client_words": lines[first][:160],
        "confidence": 0.7,
        "source": "heuristic",
    }


def validate_plan_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's plan-request report to the typed contract."""
    if not isinstance(payload, dict):
        raise VoiceOutcomeError("plan request must be an object")
    raw = payload.get("requested")
    if isinstance(raw, bool):
        requested = raw
    elif isinstance(raw, str) and raw.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
        requested = raw.strip().lower() in {"true", "yes", "1"}
    else:
        raise VoiceOutcomeError("requested must be a boolean")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        confidence = 0.5
    try:
        candidate = float(str(payload.get("initial_amount")).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        candidate = 0.0
    amount = round(candidate, 2) if math.isfinite(candidate) and candidate > 0 else None
    if not requested:
        # An amount without a request is the model volunteering a split nobody
        # asked for, so it is dropped with the request it belongs to.
        amount = None
    note = str(payload.get("note") or "").strip()[:140]
    return {
        "requested": requested,
        "initial_amount": amount,
        "note": note or (
            "The client asked to pay the amount in parts." if requested
            else "The client did not ask to pay in parts."
        ),
        "client_words": str(payload.get("client_words") or "").strip()[:160],
        "confidence": round(confidence, 2),
        "source": "llm",
    }


def detect_plan_request(
    transcript: str,
    classification: dict[str, Any] | None = None,
    caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Ask a model whether the client asked to split this debt.

    A fourth typed question over the same provider chain as the outcome
    classification, the final answer and the email decision, so all four degrade
    together. Any failure falls back to :func:`heuristic_plan_request`.

    A transcript with no client speech is answered without consulting a model at
    all: only the client can ask for a plan, so an agent-only call cannot
    contain a request no matter what a model would make of it.
    """
    if not _client_lines(transcript) or agent_only_transcript(transcript):
        return {
            "requested": False,
            "initial_amount": None,
            "note": "The client's own words were not captured, so no plan was requested.",
            "client_words": "",
            "confidence": 0.3,
            "source": "no_client_speech",
        }
    outcome = str((classification or {}).get("outcome") or "")
    briefing = (
        f"Assigned outcome (you cannot change it): {outcome or 'unknown'}\n"
        f"Today's date: {datetime.now(timezone.utc).date().isoformat()}\n\n"
        f"{transcript}"
    )
    invoke = caller or (lambda text: _call_llm(text, PLAN_REQUEST_PROMPT))
    try:
        result = validate_plan_request(_extract_json(invoke(briefing)))
    except (VoiceOutcomeError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
        return heuristic_plan_request(transcript)
    if result["requested"] and not result["client_words"]:
        # The model may summarise instead of quoting. The client's own request is
        # already in the transcript, so take it from there rather than letting
        # the record claim they asked for nothing.
        fallback = heuristic_plan_request(transcript)
        result["client_words"] = fallback["client_words"] or (_client_lines(transcript)[-1][:160])
    return result


def plan_request_hint(request: dict[str, Any] | None) -> str:
    """One line of what the client volunteered, for the chatbot to open with.

    This is the ``voice_hint`` a plan is created with. It is advisory context
    only — the chatbot still has the customer state and confirm their own
    schedule — so it is phrased as a report of what was heard, never as an
    arrangement that has been agreed.
    """
    if not (request or {}).get("requested"):
        return ""
    parts: list[str] = []
    amount = (request or {}).get("initial_amount")
    if amount:
        parts.append(f"mentioned paying Rs {float(amount):,.0f} first")
    if words := str((request or {}).get("client_words") or "").strip():
        parts.append(f'said: "{words}"')
    return " - ".join(parts) or str((request or {}).get("note") or "")


# ---------------------------------------------------------------------------
# The follow-up email — decided after the call, never before it
# ---------------------------------------------------------------------------

EMAIL_DECISION_PROMPT = """You decide one thing: whether to email a payment link to a client who has just finished a recovery call.

You are given the transcript and the outcome a separate classifier already
assigned. You cannot change that outcome, quote an amount, or write the email.
You are never asked about a client who refused to pay; that case never reaches
you.

Reply with ONE JSON object and no other text, no markdown fence, no commentary:
{
  "send_link": true or false,
  "reason": one short sentence under 160 characters explaining the decision,
  "confidence": number between 0.0 and 1.0
}

Send the link when a link is what the client needs next: they agreed to pay, or
asked for a link, or named a day they would pay.

Do NOT send when the client asked not to be emailed, said they had already paid,
asked for a person to call them back instead, or said they would pay by a route
a link does not serve, such as cash in person or a transfer they will arrange
themselves.

If the transcript is too thin to tell, send the link. The client committed to
paying, and a link is the least intrusive way to let them.
"""


def validate_email_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's email verdict to a typed contract; reject the rest."""
    if not isinstance(payload, dict):
        raise VoiceOutcomeError("email decision must be an object")
    raw = payload.get("send_link")
    if isinstance(raw, bool):
        send = raw
    elif isinstance(raw, str) and raw.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
        send = raw.strip().lower() in {"true", "yes", "1"}
    else:
        raise VoiceOutcomeError("send_link must be a boolean")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        confidence = 0.5
    reason = str(payload.get("reason") or "").strip()[:160]
    return {
        "should_send": send,
        "reason": reason or ("The client needs a payment link." if send else "A payment link is not what this client needs."),
        "confidence": round(confidence, 2),
        "source": "llm",
    }


def decide_follow_up_email(
    transcript: str,
    classification: dict[str, Any] | None = None,
    caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Ask a model whether this conversation warrants emailing the payment link.

    This is a *second* judgement, separate from the outcome classification, and
    it is advisory only: it can decline a send, but it can never cause one on its
    own. :func:`follow_up_email_for_call` refuses to consult it at all unless the
    outcome is already ``promised_to_pay``.

    With no provider reachable the answer defaults to sending, because reaching
    this function already means a promise was captured and the agent told the
    client on the call that a link would arrive.
    """
    outcome = str((classification or {}).get("outcome") or "")
    summary = str((classification or {}).get("summary") or "")
    promise = str((classification or {}).get("promise_date") or "")
    briefing = (
        f"Assigned outcome: {outcome or 'unknown'}\n"
        f"Promised date: {promise or 'none given'}\n"
        f"Classifier summary: {summary or 'none'}\n\n"
        f"{transcript}"
    )
    invoke = caller or (lambda text: _call_llm(text, EMAIL_DECISION_PROMPT))
    try:
        return validate_email_decision(_extract_json(invoke(briefing)))
    except (VoiceOutcomeError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
        return {
            "should_send": True,
            "reason": "No model could be reached; the captured promise itself justifies sending the link.",
            "confidence": 0.5,
            "source": "default",
        }


def _case_for_send(case_id: str, audit_path: Path, attempts_path: Path | None = None) -> dict[str, Any] | None:
    """Resolve the live case behind a call row, or None if there is no longer one.

    ``call_log`` stores only the case *identity* (``case_id``, ``case_key``); the
    billable facts — amount, email, condition — live in the audit log's newest
    event for that client. Reading them back through
    :class:`~modules.service_layer.RecoveryService` means the voice path bills
    exactly the case the dashboard is showing, not a stale copy of it.
    """
    from .attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH
    from .service_layer import RecoveryService

    service = RecoveryService(audit_path=audit_path, attempts_path=attempts_path or ATTEMPTS_DB_PATH)
    return next((client for client in service.list_clients() if str(client["client_id"]) == str(case_id)), None)


def follow_up_email_for_call(
    call: dict[str, Any],
    classification: dict[str, Any] | None,
    *,
    transcript: str = "",
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    auto_email: bool = True,
    email_caller: Callable[[str], str] | None = None,
    payment_client: Any = None,
    message_service: Any = None,
) -> dict[str, Any]:
    """Decide and, if warranted, send the payment link for one completed call.

    The gate is deterministic and comes first: only ``promised_to_pay`` can ever
    reach the model. ``declined`` and ``escalated`` never send, whatever a model
    says — a client who refused must not receive a bill for refusing, and an
    escalation is by definition waiting on a person.

    Returns the decision either way, in the shape the dashboard renders::

        {"should_send": bool, "sent": bool, "reason": str,
         "blocked_by"?: str, "short_url"?: str, "error"?: str}

    Nothing here raises. A call that has already been recorded must not be
    rolled back because an email failed, so a failure is returned as part of the
    decision and written to the audit trail.
    """
    outcome = str((classification or {}).get("outcome") or "")
    if outcome != "promised_to_pay":
        return {
            "should_send": False,
            "sent": False,
            "blocked_by": "outcome",
            "reason": f"A '{outcome or 'missing'}' outcome never sends an email. Only a captured promise does.",
        }
    if not auto_email:
        return {
            "should_send": False,
            "sent": False,
            "blocked_by": "auto_email_disabled",
            "reason": "Automatic sending is switched off (VOICE_AUTO_EMAIL), so the promise was recorded without a link.",
        }

    decision = decide_follow_up_email(transcript, classification, email_caller)
    result: dict[str, Any] = {"should_send": bool(decision["should_send"]), "sent": False, "reason": decision["reason"]}
    if not result["should_send"]:
        result["blocked_by"] = "agent_declined"
        return result

    case = _case_for_send(str(call.get("case_id") or ""), audit_path, attempts_path)
    if case is None:
        result["blocked_by"] = "case_not_found"
        result["reason"] = "No current case matches this call, so there is nothing to bill."
        return result
    event = dict(case.get("case") or {})
    if "@" not in str(event.get("client_email") or ""):
        result["blocked_by"] = "no_client_email"
        result["reason"] = "The client has no email address on file, so the promised link could not be delivered."
        return result

    from .handlers import handle_action

    audit_event = {
        **event,
        "event_type": "voice_follow_up_email",
        "source": f"{call.get('provider') or 'vapi'}_{call.get('mode') or 'web'}",
        "call_id": call.get("id"),
        "provider_call_id": call.get("provider_call_id") or "",
        "voice_promise_date": (classification or {}).get("promise_date") or "",
    }
    try:
        # The send runs as resend_payment_link so a real Razorpay link and
        # invoice are produced — the same action a human operator would fire,
        # and what the agent promises aloud on the call.
        handled = handle_action(
            event,
            "resend_payment_link",
            payment_client=payment_client,
            message_service=message_service,
            deliver=True,
        )
    except Exception as exc:  # noqa: BLE001 - a failed send is a recorded fact, not a crash
        result["error"] = str(exc)
        result["reason"] = f"The promised payment link could not be sent: {exc}"
        log_event(audit_event, VOICE_LINK_ACTION, None, "not_applicable", audit_path, errors=[str(exc)], outcome="technical_error", actor="voice_agent")
        return result

    result["sent"] = True
    result["short_url"] = handled.get("short_url") or ""
    log_event(
        {**audit_event, **{key: value for key, value in handled.items() if key != "message"}},
        VOICE_LINK_ACTION,
        handled.get("message"),
        "link_created" if handled.get("short_url") else "sent",
        audit_path,
        outcome=VOICE_LINK_OUTCOME,
        actor="voice_agent",
    )
    return result


# ---------------------------------------------------------------------------
# The five cards — every one a live query, none a stored counter
# ---------------------------------------------------------------------------


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def voice_metrics(
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    recovery_path: Path | None = None,
) -> dict[str, Any]:
    """Compute all five voice cards from the rows, in one cycle window.

    Card 1  ₹ recovered via voice  — SUM over recovery rows attributed to "call".
                                     A SUBSET of the dashboard's overall
                                     "Recovered" figure, never an addition to it.
    Card 2  Promises captured      — cycle-scoped count of promised_to_pay.
    Card 3  Calls placed           — cycle-scoped count of attempts (the
                                     reference window the others match).
    Card 4  Answer rate            — cycle-scoped, over COMPLETED calls only.
    Card 5  Avg time to payment    — AVG(recovered_at - recovery_triggered_at)
                                     over call-attributed recoveries. No join.
    """
    from .razorpay_webhooks import RECOVERY_DB_PATH, list_recovery_records

    cycle_start = start_of_current_cycle(audit_path)
    cycle_calls = list_calls(voice_path, since=cycle_start)

    # Card 3 — every attempt in the window, including calls still ringing.
    calls_placed = len(cycle_calls)

    # Card 2 — same window as Card 3, not all-time.
    promises = [call for call in cycle_calls if call["outcome"] == "promised_to_pay"]

    # Card 4 — completed calls only. A call with no outcome yet is not evidence.
    completed = [call for call in cycle_calls if call["outcome"]]
    reached = [call for call in completed if call["outcome"] != "no_answer"]
    answer_rate = round(len(reached) / len(completed) * 100, 1) if completed else None

    # Cards 1 and 5 — read the recovery store, which already carries the
    # attribution decision made once at webhook time.
    records = list_recovery_records(recovery_path or RECOVERY_DB_PATH)
    voice_recoveries = [record for record in records.values() if str(record.get("recovered_via") or "") == "call" and record.get("recovered_at")]
    recovered_amount = sum(float(record.get("amount_recovered") or 0) for record in voice_recoveries)
    email_recoveries = [record for record in records.values() if str(record.get("recovered_via") or "") == "email" and record.get("recovered_at")]
    total_recovered = sum(float(record.get("amount_recovered") or 0) for record in records.values() if record.get("recovered_at"))

    # Card 5 — a plain subtraction, because recovery_triggered_at was persisted
    # alongside recovered_at in the same atomic write.
    spans = []
    for record in voice_recoveries:
        start = _parse(record.get("recovery_triggered_at"))
        end = _parse(record.get("recovered_at"))
        if start and end and end > start:
            spans.append((end - start).total_seconds() / 3600)
    avg_hours = round(sum(spans) / len(spans), 1) if spans else None

    return {
        "cycle_start": cycle_start,
        # Card 1
        "recovered_via_voice": recovered_amount,
        "voice_recovery_count": len(voice_recoveries),
        "recovered_via_email": sum(float(record.get("amount_recovered") or 0) for record in email_recoveries),
        "email_recovery_count": len(email_recoveries),
        "total_recovered": total_recovered,
        # Card 2
        "promises_captured": len(promises),
        "promises_with_date": len([call for call in promises if call["promise_date"]]),
        # Card 3
        "calls_placed": calls_placed,
        "calls_in_flight": calls_placed - len(completed),
        # Card 4
        "answer_rate": answer_rate,
        "calls_completed": len(completed),
        "calls_answered": len(reached),
        # Card 5
        "avg_hours_to_payment": avg_hours,
        "avg_sample_size": len(spans),
        # Outcome split, for the panel's breakdown row.
        "outcome_counts": {outcome: len([call for call in completed if call["outcome"] == outcome]) for outcome in OUTCOMES},
    }


def record_call_audit(
    call: dict[str, Any],
    action: str,
    message: str | None,
    outcome: str,
    audit_path: Path = AUDIT_PATH,
    errors: list[str] | None = None,
) -> dict[str, str]:
    """Append the call to the immutable audit trail.

    ``call_log`` is mutable operational state (a row is updated once, when the
    call ends). History lives in the append-only audit log, so every placement
    and every classification is still permanently recorded.
    """
    event = {
        "event_type": "voice_call",
        "client_id": call.get("case_id"),
        "client_name": call.get("client_name") or "",
        "source": f"{call.get('provider') or 'vapi'}_{call.get('mode') or 'live'}",
        "call_id": call.get("id"),
        "provider_call_id": call.get("provider_call_id") or "",
        "placed_at": call.get("placed_at"),
        "ended_at": call.get("ended_at") or "",
        "call_outcome": call.get("outcome") or "",
        "promise_date": call.get("promise_date") or "",
        "transcript_summary": call.get("transcript_summary") or "",
        # The final answer is audited alongside the outcome so the permanent
        # record says what the client settled on, not just which bucket the
        # call landed in. call_log is mutable; this row is not.
        "final_answer_kind": call.get("final_answer_kind") or "",
        "final_answer": call.get("final_answer") or "",
        "final_pay_date": call.get("final_pay_date") or "",
        "client_final_words": call.get("client_final_words") or "",
    }
    return log_event(event, action, message, "not_applicable", audit_path, errors=errors, outcome=outcome, actor="voice_agent")
