"""Deterministic policy gate between LLM diagnosis and action execution.

Architectural contract
----------------------
The LLM diagnosis layer (``modules/diagnosis.py``) has **no execution
authority**. It returns a typed proposal only. Every proposal must pass through
``evaluate()`` in this module before any outbound action is allowed to fire.
Nothing in this file calls an LLM, sends a message, or creates a payment link:
it only returns a verdict plus the full list of gates that were evaluated, so
the dashboard can render exactly which rule fired for every case.

Verdict decisions
-----------------
``approve``   The action is authorised to execute now.
``defer``     The action is valid but must wait (contact window / cooldown /
              promise-to-pay). This is *not* a human escalation — the case stays
              in the automated queue and carries ``next_attempt_at``.
``escalate``  A human must take over. Always carries a machine reason code and
              a human-readable reason.

Compliance framing (see README "Compliance posture")
----------------------------------------------------
The contact window and non-harassment caps below are **self-imposed operating
policy**. They are inspired by the spirit of RBI's fair-practice principles on
contact windows and non-harassment, but this project collects commercial B2B
receivables, which is a different regulatory regime from consumer loan
recovery by Regulated Entities. No RBI compliance is claimed. The compliance
surfaces that do apply are TRAI DLT registration for bulk commercial
SMS/WhatsApp and the DPDP Act 2023 posture for customer PII.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from modules.attempt_tracker import (
    COOLDOWN_HOURS,
    MAX_ATTEMPTS,
    check_cooldown,
    get_attempt_count,
    get_next_retry_at,
    is_contact_hour_allowed,
)
from modules.attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH

ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DB_PATH = ROOT / "data" / "policy_decisions.sqlite3"

IST = ZoneInfo("Asia/Kolkata")

# --- Tunable policy thresholds (deliberately constants, not prompt text) ------
# Confidence at or above this auto-approves the proposed intervention.
CONFIDENCE_AUTO_APPROVE = 0.75
# Confidence below this goes straight to a human. Between the two bounds the
# case is escalated as "needs a judgement call" with the score in the reason.
CONFIDENCE_ESCALATE_BELOW = 0.50
# Invoices above this INR value always route to a human regardless of how
# confident the model is. Money size, not model certainty, drives this gate.
AMOUNT_HUMAN_REVIEW_THRESHOLD = 50000.0
# Self-imposed outreach window in IST.
CONTACT_WINDOW_START_HOUR = 8
CONTACT_WINDOW_END_HOUR = 22
# A case cannot remain in automated recovery indefinitely.
MAX_RECOVERY_WINDOW_DAYS = 14
# Retry ladder measured from the preceding successful attempt: ~24h, 72h, 7d.
RETRY_LADDER_HOURS = (24, 72, 168)

PAYMENT_ACTIONS = frozenset({"charge_fee", "retry_payment", "resend_payment_link"})
MESSAGE_ACTIONS = frozenset({"friendly_reminder", "firm_reminder", "final_notice", "offer_waitlist"})
ALLOWED_ACTIONS = PAYMENT_ACTIONS | MESSAGE_ACTIONS
ESCALATION_ACTION = "escalate_human"

# Machine reason codes -> operator-facing copy template. Every escalation and
# every deferral in the audit trail carries one of these.
REASON_CODES: dict[str, str] = {
    "auto_approved": "Auto-approved: confidence {confidence:.2f} at or above {threshold:.2f} threshold",
    "validation_error": "Event data failed validation: {detail}",
    "invalid_proposal": "Diagnosis output rejected by schema gate: {detail}",
    "unsupported_action": "Proposed action '{detail}' is not in the bounded executor allow-list",
    "low_confidence": "Confidence {confidence:.2f} below {threshold:.2f} floor",
    "confidence_review_band": "Confidence {confidence:.2f} in review band {floor:.2f}-{threshold:.2f}",
    "amount_above_threshold": "Amount {amount} above {threshold} auto-action ceiling",
    "attempt_limit": "attempt {attempts} of {max_attempts} reached with no response - escalated to human review",
    "recovery_window_expired": "Automated recovery window expired after {days:.1f} days (maximum {max_days} days)",
    "hard_decline_blind_retry": "Hard decline cannot be blindly retried; payment-method update flow required",
    "soft_decline_link_mismatch": "Soft decline should enter the scheduled retry ladder, not a payment-method replacement flow",
    "contact_opt_out": "Customer has opted out of automated outreach",
    "outside_contact_window": "Outside self-imposed contact window ({start}:00-{end}:00 IST)",
    "cooldown_active": "Cooldown active: {hours}h between attempts, next window {next_at}",
    "promise_to_pay": "Promise-to-pay recorded until {detail} — outreach suppressed",
    "duplicate_suppressed": "Already actioned this cycle under idempotency key {detail}",
    "amount_below_cost_floor": "Amount {amount} below cost-to-collect floor {threshold}",
}


def describe_reason(code: str, **params: Any) -> str:
    """Render an operator-facing reason string for a machine reason code."""
    template = REASON_CODES.get(code)
    if not template:
        return code.replace("_", " ")
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return code.replace("_", " ")


@dataclass(frozen=True)
class PolicyCheck:
    """One gate evaluation, recorded whether it passed or failed."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class PolicyVerdict:
    """The complete, auditable output of the deterministic gate."""

    decision: str  # approve | defer | escalate
    action: str
    reason_code: str
    reason: str
    idempotency_key: str
    confidence: float
    attempt_number: int
    max_attempts: int = MAX_ATTEMPTS
    contact_window_ok: bool = True
    next_attempt_at: str | None = None
    checks: tuple[PolicyCheck, ...] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    @property
    def escalated(self) -> bool:
        return self.decision == "escalate"

    @property
    def deferred(self) -> bool:
        return self.decision == "defer"

    @property
    def executable_action(self) -> str:
        """The action the bounded executor is allowed to run for this verdict."""
        return self.action if self.decision == "approve" else ESCALATION_ACTION

    def badge(self) -> str:
        """Short per-case UI badge, e.g. 'Attempt 2 of 3 - Contact window OK'."""
        parts = [f"Attempt {self.attempt_number} of {self.max_attempts}"]
        parts.append("Contact window OK" if self.contact_window_ok else "Outside contact window")
        if self.decision == "approve":
            parts.append(f"Escalates after attempt {self.max_attempts}")
        elif self.decision == "defer" and self.next_attempt_at:
            parts.append(f"Retries {self.next_attempt_at[:16].replace('T', ' ')}")
        else:
            parts.append("Human review")
        return " • ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "action": self.action,
            "executable_action": self.executable_action,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "confidence": round(float(self.confidence), 4),
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
            "contact_window_ok": self.contact_window_ok,
            "next_attempt_at": self.next_attempt_at,
            "checks": [check.to_dict() for check in self.checks],
            "badge": self.badge(),
        }


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _cycle_id(now: datetime | None = None) -> str:
    """Return the current outreach cycle identifier (one calendar day, IST)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    return moment.strftime("%Y-%m-%d")


def idempotency_key(event: dict[str, Any], action: str, now: datetime | None = None) -> str:
    """Return a stable per-case, per-action, per-cycle idempotency key.

    The same invoice cannot be contacted twice for the same action inside one
    cycle, no matter how many times the batch runner is re-executed.
    """
    identity = {
        "client_id": str(event.get("client_id") or "").strip().lower(),
        "event_type": str(event.get("event_type") or "").strip().lower(),
        "reference": str(
            event.get("invoice_id")
            or event.get("payment_id")
            or event.get("subscription_id")
            or event.get("payment_link_id")
            or event.get("appointment_datetime")
            or event.get("last_charge_date")
            or ""
        ).strip(),
        "action": str(action or "").strip().lower(),
        "cycle": _cycle_id(now),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return f"pol_{digest[:24]}"


def _connect(path: Path = DECISIONS_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_decisions (
            idempotency_key TEXT PRIMARY KEY,
            client_id       TEXT NOT NULL,
            action          TEXT NOT NULL,
            decision        TEXT NOT NULL,
            reason_code     TEXT NOT NULL,
            confidence      REAL,
            cycle           TEXT NOT NULL,
            created_at      TEXT NOT NULL
        )
        """
    )
    return connection


def reserve_key(
    key: str,
    client_id: str,
    action: str,
    db_path: Path = DECISIONS_DB_PATH,
    now: datetime | None = None,
) -> bool:
    """Claim an idempotency key. Returns False when this cycle already ran it."""
    if not str(key or "").strip():
        raise ValueError("idempotency key is required")
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO policy_decisions
                (idempotency_key, client_id, action, decision, reason_code, confidence, cycle, created_at)
            VALUES (?, ?, ?, 'reserved', 'pending', NULL, ?, ?)
            """,
            (str(key), str(client_id or "unknown"), str(action), _cycle_id(now), timestamp),
        )
        return cursor.rowcount == 1


def key_exists(key: str, db_path: Path = DECISIONS_DB_PATH) -> bool:
    """Return True when the idempotency key has already been claimed."""
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM policy_decisions WHERE idempotency_key = ?", (str(key),)
        ).fetchone()
    return row is not None


def record_verdict(
    verdict: PolicyVerdict,
    client_id: str,
    db_path: Path = DECISIONS_DB_PATH,
    now: datetime | None = None,
) -> None:
    """Persist the final verdict against its idempotency key."""
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO policy_decisions
                (idempotency_key, client_id, action, decision, reason_code, confidence, cycle, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                decision = excluded.decision,
                reason_code = excluded.reason_code,
                confidence = excluded.confidence
            """,
            (
                verdict.idempotency_key,
                str(client_id or "unknown"),
                verdict.action,
                verdict.decision,
                verdict.reason_code,
                float(verdict.confidence),
                _cycle_id(now),
                timestamp,
            ),
        )


def release_key(key: str, db_path: Path = DECISIONS_DB_PATH) -> None:
    """Release an unexecuted key after a provider failure.

    Approval is persisted before execution for auditability, so limiting deletion
    to the transient ``reserved`` state would incorrectly burn a failed cycle.
    """
    with _connect(db_path) as connection:
        connection.execute(
            "DELETE FROM policy_decisions WHERE idempotency_key = ?",
            (str(key),),
        )


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _amount(event: dict[str, Any], proposal: dict[str, Any]) -> float:
    for source in (proposal, event):
        for key in ("amount", "amount_at_risk", "fee_amount", "invoice_amount", "appointment_value", "subscription_amount"):
            try:
                amount = float(source.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount) and amount > 0:
                return round(amount, 2)
    return 0.0


def _is_opted_out(event: dict[str, Any]) -> bool:
    for key in ("opt_out", "do_not_contact", "dnd", "unsubscribed"):
        value = event.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif str(value or "").strip().lower() in {"true", "1", "yes", "y"}:
            return True
    return False


def next_contact_window_open(now: datetime | None = None) -> str:
    """Return the ISO timestamp (UTC) when the self-imposed window next opens."""
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    target = moment.replace(hour=CONTACT_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if moment.hour >= CONTACT_WINDOW_END_HOUR or moment >= target:
        if moment.hour >= CONTACT_WINDOW_END_HOUR:
            target = target + timedelta(days=1)
        elif moment.hour >= CONTACT_WINDOW_START_HOUR:
            target = moment
    return target.astimezone(timezone.utc).isoformat()


def _promise_to_pay_active(event: dict[str, Any], now: datetime | None = None) -> str | None:
    """Return the promised ISO date when a future promise-to-pay suppresses contact."""
    raw = str(event.get("promise_to_pay_date") or event.get("promised_payment_date") or "").strip()
    if not raw:
        return None
    try:
        promised = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if promised.tzinfo is None:
        promised = promised.replace(tzinfo=timezone.utc)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return promised.isoformat() if promised > reference else None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def evaluate(
    event: dict[str, Any],
    proposal: dict[str, Any],
    attempts_path: Path = ATTEMPTS_DB_PATH,
    decisions_path: Path = DECISIONS_DB_PATH,
    now: datetime | None = None,
    enforce_idempotency: bool = True,
    cost_floor: float = 0.0,
) -> PolicyVerdict:
    """Gate one LLM proposal against deterministic policy.

    ``event`` is a normalized RevenueEvent. ``proposal`` is the typed diagnosis
    output (``recommended_intervention``, ``confidence``, ``root_cause``, ...).
    The proposal is advisory only: this function decides what may actually run.
    """
    checks: list[PolicyCheck] = []
    client_id = str(event.get("client_id") or "unknown")
    raw_action = str(proposal.get("recommended_intervention") or proposal.get("action") or "").strip()
    confidence = _confidence(proposal.get("confidence"))
    amount = _amount(event, proposal)
    key = idempotency_key(event, raw_action or "unresolved", now)

    attempt_baseline = 0
    raw_baseline = event.get("attempt_count")
    if isinstance(raw_baseline, int) and not isinstance(raw_baseline, bool):
        attempt_baseline = max(0, raw_baseline)
    tracked = get_attempt_count(client_id, attempts_path, action_scope="payment") if client_id != "unknown" else 0
    attempts_so_far = max(attempt_baseline, tracked)
    attempt_number = attempts_so_far + 1
    window_ok = is_contact_hour_allowed(now)

    def verdict(decision: str, action: str, code: str, **params: Any) -> PolicyVerdict:
        result = PolicyVerdict(
            decision=decision,
            action=action,
            reason_code=code,
            reason=describe_reason(code, **params),
            idempotency_key=key,
            confidence=confidence if confidence is not None else 0.0,
            attempt_number=min(attempt_number, MAX_ATTEMPTS),
            contact_window_ok=window_ok,
            next_attempt_at=params.get("next_attempt_at"),
            checks=tuple(checks),
        )
        if client_id != "unknown":
            record_verdict(result, client_id, decisions_path, now)
        return result

    # 1. Event data integrity. Bad data never reaches an executor.
    validation_errors = [str(item) for item in (event.get("validation_errors") or [])]
    checks.append(PolicyCheck("data_validation", not validation_errors, "; ".join(validation_errors)))
    if validation_errors:
        return verdict("escalate", ESCALATION_ACTION, "validation_error", detail="; ".join(validation_errors))

    # 2. Proposal schema. An unparsable or off-menu proposal is not actionable.
    schema_error = ""
    if not raw_action:
        schema_error = "missing recommended_intervention"
    elif confidence is None:
        schema_error = "confidence missing or outside 0.0-1.0"
    checks.append(PolicyCheck("proposal_schema", not schema_error, schema_error))
    if schema_error:
        return verdict("escalate", ESCALATION_ACTION, "invalid_proposal", detail=schema_error)

    # 3. Bounded executor allow-list. The model cannot invent capabilities.
    if raw_action == ESCALATION_ACTION:
        checks.append(PolicyCheck("action_allow_list", True, "model requested human handoff"))
        return verdict(
            "escalate",
            ESCALATION_ACTION,
            "low_confidence" if (confidence or 0.0) < CONFIDENCE_ESCALATE_BELOW else "confidence_review_band",
            confidence=confidence or 0.0,
            threshold=CONFIDENCE_AUTO_APPROVE,
            floor=CONFIDENCE_ESCALATE_BELOW,
        )
    allowed = raw_action in ALLOWED_ACTIONS
    checks.append(PolicyCheck("action_allow_list", allowed, raw_action))
    if not allowed:
        return verdict("escalate", ESCALATION_ACTION, "unsupported_action", detail=raw_action)

    # 4. Consent / opt-out. Checked before any other outreach gate (DPDP posture).
    opted_out = _is_opted_out(event)
    checks.append(PolicyCheck("consent_opt_out", not opted_out, "opt-out flag set" if opted_out else "no opt-out on record"))
    if opted_out:
        return verdict("escalate", ESCALATION_ACTION, "contact_opt_out")

    # 5. Confidence floor and review band.
    floor_ok = confidence >= CONFIDENCE_ESCALATE_BELOW
    checks.append(PolicyCheck("confidence_floor", floor_ok, f"{confidence:.2f} vs {CONFIDENCE_ESCALATE_BELOW:.2f}"))
    if not floor_ok:
        return verdict("escalate", ESCALATION_ACTION, "low_confidence", confidence=confidence, threshold=CONFIDENCE_ESCALATE_BELOW)
    auto_ok = confidence >= CONFIDENCE_AUTO_APPROVE
    checks.append(PolicyCheck("confidence_auto_approve", auto_ok, f"{confidence:.2f} vs {CONFIDENCE_AUTO_APPROVE:.2f}"))
    if not auto_ok:
        return verdict(
            "escalate",
            ESCALATION_ACTION,
            "confidence_review_band",
            confidence=confidence,
            threshold=CONFIDENCE_AUTO_APPROVE,
            floor=CONFIDENCE_ESCALATE_BELOW,
        )

    # 6. Amount ceiling. Large money always sees a human.
    amount_ok = amount <= AMOUNT_HUMAN_REVIEW_THRESHOLD
    checks.append(PolicyCheck("amount_ceiling", amount_ok, f"INR {amount:,.2f} vs INR {AMOUNT_HUMAN_REVIEW_THRESHOLD:,.2f}"))
    if not amount_ok:
        return verdict(
            "escalate",
            ESCALATION_ACTION,
            "amount_above_threshold",
            amount=f"INR {amount:,.0f}",
            threshold=f"INR {AMOUNT_HUMAN_REVIEW_THRESHOLD:,.0f}",
        )

    # 7. Cost-to-collect floor. Chasing tiny balances costs more than it returns.
    floor_clear = cost_floor <= 0 or amount >= cost_floor
    checks.append(PolicyCheck("cost_to_collect_floor", floor_clear, f"INR {amount:,.2f} vs INR {cost_floor:,.2f}"))
    if not floor_clear:
        return verdict(
            "escalate",
            ESCALATION_ACTION,
            "amount_below_cost_floor",
            amount=f"INR {amount:,.0f}",
            threshold=f"INR {cost_floor:,.0f}",
        )

    # 8. Failure-class guard. Hard declines update the payment method; soft
    # declines follow the retry ladder. The model cannot reverse this branch.
    decline_class = str(event.get("decline_class") or "unknown").strip().lower()
    decline_action_ok = not (
        (decline_class == "hard" and raw_action == "retry_payment")
        or (decline_class == "soft" and raw_action == "resend_payment_link")
    )
    checks.append(PolicyCheck("decline_action_match", decline_action_ok, f"{decline_class} decline → {raw_action}"))
    if not decline_action_ok:
        code = "hard_decline_blind_retry" if decline_class == "hard" else "soft_decline_link_mismatch"
        return verdict("escalate", ESCALATION_ACTION, code)

    # 9. Maximum recovery window. This closes old cases even if counters were
    # reset or webhook delivery was delayed.
    try:
        case_age = float(event.get("aging_days")) if event.get("aging_days") is not None else 0.0
    except (TypeError, ValueError):
        case_age = 0.0
    window_clear = case_age <= MAX_RECOVERY_WINDOW_DAYS
    checks.append(PolicyCheck("recovery_window", window_clear, f"{case_age:.1f}/{MAX_RECOVERY_WINDOW_DAYS} days"))
    if not window_clear:
        return verdict(
            "escalate", ESCALATION_ACTION, "recovery_window_expired",
            days=case_age, max_days=MAX_RECOVERY_WINDOW_DAYS,
        )

    # 10. Attempt cap. MAX_ATTEMPTS includes the current proposed attempt, so a
    # case with two completed attempts is escalated instead of sending a third.
    cap_ok = attempt_number < MAX_ATTEMPTS
    checks.append(PolicyCheck("attempt_cap", cap_ok, f"attempt {min(attempt_number, MAX_ATTEMPTS)} of {MAX_ATTEMPTS}"))
    if not cap_ok:
        return verdict(
            "escalate",
            ESCALATION_ACTION,
            "attempt_limit",
            attempts=min(attempt_number, MAX_ATTEMPTS),
            max_attempts=MAX_ATTEMPTS,
        )

    # 11. Promise-to-pay suppression. A kept promise deserves silence.
    promised = _promise_to_pay_active(event, now)
    checks.append(PolicyCheck("promise_to_pay", promised is None, promised or "no active promise"))
    if promised:
        return verdict("defer", raw_action, "promise_to_pay", detail=promised[:10], next_attempt_at=promised)

    # 12. Self-imposed contact window. A quiet-hour hold is a deferral, not an
    #     escalation: the case stays automated and retries when the window opens.
    checks.append(PolicyCheck("contact_window", window_ok, f"{CONTACT_WINDOW_START_HOUR}:00-{CONTACT_WINDOW_END_HOUR}:00 IST"))
    if not window_ok:
        reopen = next_contact_window_open(now)
        return verdict(
            "defer",
            raw_action,
            "outside_contact_window",
            start=CONTACT_WINDOW_START_HOUR,
            end=CONTACT_WINDOW_END_HOUR,
            next_attempt_at=reopen,
        )

    # 13. Retry ladder between consecutive payment attempts. The persisted
    # timestamp is combined with the current rung (24h, 72h, then 7d).
    ladder_hours = RETRY_LADDER_HOURS[min(attempts_so_far, len(RETRY_LADDER_HOURS) - 1)]
    # Explicit historical timestamps (webhook/production cases) enforce wall
    # clock scheduling. Legacy backfill rows without one remain replayable for
    # deterministic demos and backwards-compatible batch tests.
    has_failure_timestamp = bool(event.get("occurred_at") or event.get("last_charge_date"))
    in_cooldown = bool(has_failure_timestamp and client_id != "unknown" and raw_action in PAYMENT_ACTIONS and check_cooldown(client_id, attempts_path, action_scope="payment"))
    next_retry = get_next_retry_at(client_id, attempts_path, action_scope="payment") if in_cooldown else None
    if in_cooldown and next_retry and ladder_hours != COOLDOWN_HOURS:
        try:
            base = datetime.fromisoformat(next_retry) - timedelta(hours=COOLDOWN_HOURS)
            next_retry = (base + timedelta(hours=ladder_hours)).isoformat()
            in_cooldown = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) < datetime.fromisoformat(next_retry).astimezone(timezone.utc)
        except ValueError:
            pass
    checks.append(PolicyCheck("retry_ladder", not in_cooldown, f"rung {attempt_number}: {ladder_hours}h gap"))
    if in_cooldown:
        return verdict(
            "defer",
            raw_action,
            "cooldown_active",
            hours=ladder_hours,
            next_at=(next_retry or "")[:16].replace("T", " "),
            next_attempt_at=next_retry,
        )

    # 14. Idempotency. Claimed last so a rejected case does not burn its key.
    if enforce_idempotency:
        claimed = reserve_key(key, client_id, raw_action, decisions_path, now)
        checks.append(PolicyCheck("idempotency", claimed, key))
        if not claimed:
            return verdict("defer", raw_action, "duplicate_suppressed", detail=key)
    else:
        checks.append(PolicyCheck("idempotency", True, "not enforced for this run"))

    return verdict(
        "approve",
        raw_action,
        "auto_approved",
        confidence=confidence,
        threshold=CONFIDENCE_AUTO_APPROVE,
    )


if __name__ == "__main__":
    import tempfile

    # ignore_cleanup_errors: SQLite WAL handles can outlive the block on Windows.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        decisions = Path(tmp) / "decisions.sqlite3"
        attempts = Path(tmp) / "attempts.sqlite3"
        daytime = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
        night = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)  # 01:30 IST

        cases: list[tuple[str, dict[str, Any], dict[str, Any], datetime, str, str]] = [
            (
                "high confidence auto-approves",
                {"client_id": "C1", "event_type": "payment_failed", "invoice_id": "inv_1", "amount": 2400},
                {"recommended_intervention": "retry_payment", "confidence": 0.91},
                daytime,
                "approve",
                "auto_approved",
            ),
            (
                "low confidence escalates",
                {"client_id": "C2", "event_type": "payment_failed", "invoice_id": "inv_2", "amount": 2400},
                {"recommended_intervention": "retry_payment", "confidence": 0.42},
                daytime,
                "escalate",
                "low_confidence",
            ),
            (
                "review band escalates",
                {"client_id": "C3", "event_type": "payment_failed", "invoice_id": "inv_3", "amount": 2400},
                {"recommended_intervention": "retry_payment", "confidence": 0.61},
                daytime,
                "escalate",
                "confidence_review_band",
            ),
            (
                "large amount escalates despite confidence",
                {"client_id": "C4", "event_type": "invoice_overdue", "invoice_id": "inv_4", "amount": 75000},
                {"recommended_intervention": "retry_payment", "confidence": 0.99},
                daytime,
                "escalate",
                "amount_above_threshold",
            ),
            (
                "quiet hours defer, not escalate",
                {"client_id": "C5", "event_type": "payment_failed", "invoice_id": "inv_5", "amount": 2400},
                {"recommended_intervention": "retry_payment", "confidence": 0.9},
                night,
                "defer",
                "outside_contact_window",
            ),
            (
                "off-menu action rejected",
                {"client_id": "C6", "event_type": "payment_failed", "invoice_id": "inv_6", "amount": 100},
                {"recommended_intervention": "wire_transfer_funds", "confidence": 0.99},
                daytime,
                "escalate",
                "unsupported_action",
            ),
            (
                "opt-out respected",
                {"client_id": "C7", "event_type": "payment_failed", "invoice_id": "inv_7", "amount": 100, "opt_out": True},
                {"recommended_intervention": "retry_payment", "confidence": 0.99},
                daytime,
                "escalate",
                "contact_opt_out",
            ),
            (
                "bad data escalates",
                {"client_id": "C8", "event_type": "payment_failed", "validation_errors": ["missing amount"]},
                {"recommended_intervention": "retry_payment", "confidence": 0.99},
                daytime,
                "escalate",
                "validation_error",
            ),
        ]

        failures = 0
        for label, event, proposal, moment, expected_decision, expected_code in cases:
            result = evaluate(event, proposal, attempts_path=attempts, decisions_path=decisions, now=moment)
            ok = result.decision == expected_decision and result.reason_code == expected_code
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'} {label}: {result.decision}/{result.reason_code} — {result.reason}")

        repeat_event = {"client_id": "C1", "event_type": "payment_failed", "invoice_id": "inv_1", "amount": 2400}
        repeat = evaluate(
            repeat_event,
            {"recommended_intervention": "retry_payment", "confidence": 0.91},
            attempts_path=attempts,
            decisions_path=decisions,
            now=daytime,
        )
        ok = repeat.decision == "defer" and repeat.reason_code == "duplicate_suppressed"
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} idempotency blocks a repeat in the same cycle: {repeat.decision}/{repeat.reason_code}")

        if failures:
            raise SystemExit(1)
