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
  call, an outbound phone call and a Demo Mode call converge on the exact same
  two steps:

      step 1  answered?  -> no  => outcome = "no_answer", classification skipped
                         -> yes => step 2
      step 2  the captured speech goes through the SAME typed-JSON 4-way
              classification, which may only return an ANSWERED outcome.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .audit_log import AUDIT_PATH, log_event

ROOT = Path(__file__).resolve().parents[1]
VOICE_DB_PATH = ROOT / "data" / "voice_calls.sqlite3"

# The closed outcome enum. Nothing outside this tuple may ever be stored.
OUTCOMES = ("promised_to_pay", "declined", "no_answer", "escalated")

# How the attempt was made. This is transport, not outcome: no card branches on
# it, so a demo run and a real call are counted by identical queries.
#   web   - Vapi web call in the operator's browser (the primary flow)
#   live  - outbound telephony call placed through Vapi
#   demo  - simulated, no provider contacted
CALL_MODES = ("web", "live", "demo")

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
        "  ended_reason TEXT NOT NULL DEFAULT ''"
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
    path: Path = VOICE_DB_PATH,
) -> dict[str, Any]:
    """Write the terminal facts of one attempt in a single atomic UPDATE.

    ``outcome``, ``answered``, ``ended_at`` and ``promise_date`` always land
    together. A partially closed row (ended but unclassified, or classified but
    not ended) is therefore unreachable, so no card has to defend against one.
    """
    if outcome not in OUTCOMES:
        raise VoiceOutcomeError(f"outcome '{outcome}' is not one of {OUTCOMES}")
    if answered and outcome == "no_answer":
        raise VoiceOutcomeError("an answered call cannot have outcome 'no_answer'")
    if not answered and outcome != "no_answer":
        raise VoiceOutcomeError("an unanswered call must have outcome 'no_answer'")
    promise = str(promise_date or "") if outcome == "promised_to_pay" else ""
    with _connect(path) as connection:
        cursor = connection.execute(
            "UPDATE call_log SET ended_at = ?, outcome = ?, answered = ?, promise_date = ?, "
            "transcript_summary = ?, ended_reason = ? WHERE id = ? AND ended_at = ''",
            (_now(), outcome, "true" if answered else "false", promise, str(transcript_summary or ""), str(ended_reason or ""), int(call_id)),
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


def _call_llm(transcript: str) -> str:
    """Ask the configured provider for a typed classification. Groq, then Gemini."""
    import requests

    messages = [
        {"role": "system", "content": CLASSIFIER_PROMPT},
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
                json={"contents": [{"parts": [{"text": f"{CLASSIFIER_PROMPT}\n\nCall transcript:\n{transcript}"}]}], "generationConfig": {"temperature": 0.1}},
                timeout=30,
            )
            response.raise_for_status()
            return str(response.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Gemini: {exc}")
    raise RuntimeError("; ".join(errors) or "no LLM provider configured")


def classify_reply(transcript: str, caller: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Step 2 of the two-step outcome rule: label an ANSWERED call's reply.

    Identical for a web call, a phone call and a Demo Mode call — that sameness
    is the point. Any model failure degrades to :func:`heuristic_outcome`, so the
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
) -> dict[str, Any]:
    """Run the full two-step rule and return a closable outcome payload.

    Step 1 (``answered``) is decided by the caller — the silence window for a web
    or demo call, or Vapi's ``endedReason`` on an outbound phone call. Step 2 only
    happens when step 1 said yes.
    """
    if not answered:
        return {"outcome": "no_answer", "promise_date": None, "summary": "Nobody picked up.", "confidence": 1.0, "source": "silence_window" if not ended_reason else ended_reason, "answered": False}
    classified = classify_reply(transcript, caller)
    return {**classified, "answered": True}


def answered_from_ended_reason(ended_reason: str, transcript: str = "") -> bool:
    """Map a provider hangup reason to step 1's yes/no, transcript as tiebreak."""
    reason = str(ended_reason or "").strip().lower()
    if reason in UNANSWERED_REASONS:
        return False
    return bool(str(transcript or "").strip())


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
    }
    return log_event(event, action, message, "not_applicable", audit_path, errors=errors, outcome=outcome, actor="voice_agent")
