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
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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

# --- Flexible payment plan rules (merchant-configured defaults) --------------
# A customer proposes an installment schedule conversationally in the chatbot,
# but the conversation has no authority: only ``evaluate_plan_schedule()`` below
# decides whether a schedule may be confirmed. Each default is overridable per
# deployment through the matching environment variable of the same name.
PLAN_MAX_INSTALLMENTS = 3
PLAN_MAX_EXTENSION_DAYS = 30
PLAN_MIN_INSTALLMENT_AMOUNT = 500.0
# The payment due now must clear this share of the original amount.
PLAN_MIN_FIRST_PAYMENT_RATIO = 0.20
# The smallest installment the payment provider will actually accept. Razorpay
# will not mint a link below one rupee, so no policy relaxation may go under it.
#
# This exists because ``PLAN_MIN_INSTALLMENT_AMOUNT`` is a *cost-to-collect*
# floor written for four-figure debts. Applied literally to a small debt it is
# not a floor but a prohibition: on a 199 rupee balance a 500 rupee minimum
# cannot be met by any installment, so every split was rejected and the
# advertised "first payment of at least Rs 199" asked for the entire debt in a
# sentence offering to divide it. The floors are therefore scaled to the debt
# (see :func:`effective_min_installment`) rather than clamped onto it.
PLAN_ABSOLUTE_MIN_INSTALLMENT = 1.0
PLAN_ALLOW_PARTIAL_PAYMENT = True
PLAN_ALLOW_FUTURE_DATES = True
# Settling for less than the amount owed is a commercial decision, not an
# automated one. A short schedule is rejected rather than silently discounted.
PLAN_ALLOW_DISCOUNTS = False
# Rounding slack when comparing a proposed schedule against the amount due.
PLAN_TOTAL_TOLERANCE = 0.5

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
    # Flexible payment plan schedule gates. These are customer-facing: the
    # chatbot reads the rendered reason back to the person who proposed the plan.
    "plan_approved": "Plan accepted: {count} installment(s) totalling {total}",
    "plan_empty": "No payment schedule was proposed",
    "plan_amount_unknown": "This plan cannot be checked without the original amount due",
    "plan_too_many_installments": "{count} installments proposed, and at most {max_count} are allowed",
    "plan_partial_not_allowed": "Part payments are not available on this account, so the full amount is due in one payment",
    "plan_installment_too_small": "Payment {index} of {amount} is below the {threshold} minimum for an installment",
    "plan_first_payment_too_small": "The payment due now, {amount}, is below the {threshold} minimum first payment",
    "plan_total_short": "The schedule totals {total}, which is short of the {required} due",
    "plan_total_excess": "The schedule totals {total}, which is more than the {required} due",
    "plan_invalid_date": "Payment {index} has a date that could not be read: {detail}",
    "plan_due_date_past": "Payment {index} is dated {detail}, which has already passed",
    "plan_dates_out_of_order": "Payment {index} is dated {detail}, which is not after the payment before it",
    "plan_future_dates_not_allowed": "Deferred dates are not available on this account, so the full amount is due today",
    "plan_extension_too_long": "The final payment on {detail} is {days} days away, beyond the {max_days}-day maximum",
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


# ---------------------------------------------------------------------------
# The flexible-plan gate
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(str(os.getenv(name) or "").strip())
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if raw in {"true", "1", "yes", "y", "on"}:
        return True
    if raw in {"false", "0", "no", "n", "off"}:
        return False
    return default


def plan_policy() -> dict[str, Any]:
    """Return the merchant's flexible-plan rules as plain data.

    The chatbot is told these numbers so it can negotiate inside them, and the
    customer-facing page can display them. Reading the rules is not the same as
    applying them: ``evaluate_plan_schedule()`` remains the only decider.
    """
    return {
        "max_installments": _env_int("PLAN_MAX_INSTALLMENTS", PLAN_MAX_INSTALLMENTS),
        "max_extension_days": _env_int("PLAN_MAX_EXTENSION_DAYS", PLAN_MAX_EXTENSION_DAYS),
        "min_installment_amount": _env_float("PLAN_MIN_INSTALLMENT_AMOUNT", PLAN_MIN_INSTALLMENT_AMOUNT),
        "absolute_min_installment": _env_float("PLAN_ABSOLUTE_MIN_INSTALLMENT", PLAN_ABSOLUTE_MIN_INSTALLMENT),
        "min_first_payment_ratio": _env_float("PLAN_MIN_FIRST_PAYMENT_RATIO", PLAN_MIN_FIRST_PAYMENT_RATIO),
        "partial_payment_allowed": _env_flag("PLAN_ALLOW_PARTIAL_PAYMENT", PLAN_ALLOW_PARTIAL_PAYMENT),
        "future_dates_allowed": _env_flag("PLAN_ALLOW_FUTURE_DATES", PLAN_ALLOW_FUTURE_DATES),
        "discounts_allowed": _env_flag("PLAN_ALLOW_DISCOUNTS", PLAN_ALLOW_DISCOUNTS),
    }


def _plan_money(value: Any) -> float:
    """Round to whole paise; anything unreadable is zero, never an exception."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number, 2) if math.isfinite(number) and number > 0 else 0.0


def effective_min_installment(original_amount: Any, policy: dict[str, Any] | None = None) -> float:
    """The per-installment floor actually applied to one specific debt.

    ``min_installment_amount`` is a cost-to-collect figure: below it, minting and
    chasing a payment link costs more than the installment recovers. That
    reasoning holds for four-figure debts and collapses on small ones. A 500
    rupee floor on a 199 rupee balance is not a floor, it is a prohibition — no
    division of 199 can put 500 in every part, so every schedule was rejected
    while the assistant went on inviting the customer to propose one.

    The floor is therefore capped at the largest value an even split across the
    full installment allowance could still satisfy, and never falls below what
    the payment provider will accept. Whole rupees, because a cap of 66.33 would
    reject the only sane three-way split of 199. On a debt large enough for the
    configured minimum to be reachable the cap does not bind and the merchant's
    own figure is returned unchanged.
    """
    rules = policy or plan_policy()
    amount = _plan_money(original_amount)
    configured = _plan_money(rules.get("min_installment_amount"))
    provider_floor = _plan_money(rules.get("absolute_min_installment")) or PLAN_ABSOLUTE_MIN_INSTALLMENT
    if amount <= 0:
        return round(configured, 2)
    max_count = max(int(rules.get("max_installments") or 1), 1)
    reachable = float(math.floor(amount / max_count))
    return round(max(min(configured, reachable), min(provider_floor, amount)), 2)


def min_first_payment(original_amount: Any, policy: dict[str, Any] | None = None) -> float:
    """Smallest acceptable payment-due-now for a multi-installment schedule.

    Built on :func:`effective_min_installment` so the advertised figure is always
    one the customer can actually pay while still leaving a balance to defer. It
    can no longer equal the whole debt, which is what made the offer to split a
    small balance self-contradicting.
    """
    rules = policy or plan_policy()
    amount = _plan_money(original_amount)
    floor = max(
        effective_min_installment(amount, rules),
        round(amount * float(rules.get("min_first_payment_ratio") or 0.0), 2),
    )
    # Never demand more than the debt itself, however the ratios are configured.
    return round(min(floor, amount), 2) if amount > 0 else round(floor, 2)


def _plan_date(value: Any, today: date) -> date | None:
    """Parse one proposed due date. Blank and 'today' both mean today; None is unreadable."""
    text = str(value or "").strip().lower()
    if not text or text in {"today", "now", "immediately", "asap"}:
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    for candidate in (text, text[:10]):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def _plan_rows(installments: Any) -> list[dict[str, Any]]:
    """Normalize a proposed schedule to ``[{index, amount, due_date}]``, in order."""
    rows: list[dict[str, Any]] = []
    for item in installments if isinstance(installments, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        amount = _plan_money(item.get("amount"))
        if amount <= 0:
            continue
        rows.append(
            {
                "index": len(rows) + 1,
                "amount": amount,
                "due_date": str(item.get("due_date") or "").strip(),
            }
        )
    return rows


@dataclass(frozen=True)
class PlanVerdict:
    """The complete, auditable verdict on one customer-proposed schedule.

    ``approve`` the schedule may be confirmed and its first link created.
    ``revise``  the schedule is outside policy; ``reason`` explains why in copy
                the chatbot can read back verbatim so the customer can retry.
    """

    decision: str  # approve | revise
    reason_code: str
    reason: str
    original_amount: float
    installments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    checks: tuple[PolicyCheck, ...] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    @property
    def total(self) -> float:
        return round(sum(float(row.get("amount") or 0) for row in self.installments), 2)

    @property
    def due_now(self) -> float:
        return round(float(self.installments[0].get("amount") or 0), 2) if self.installments else 0.0

    @property
    def remaining(self) -> float:
        """Balance still owed after the first payment clears."""
        return round(max(self.total - self.due_now, 0.0), 2)

    @property
    def shortfall(self) -> float:
        """How far the schedule falls short of the amount due (0.0 when it does not)."""
        return round(max(float(self.original_amount) - self.total, 0.0), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "approved": self.approved,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "original_amount": round(float(self.original_amount), 2),
            "total": self.total,
            "due_now": self.due_now,
            "remaining": self.remaining,
            "shortfall": self.shortfall,
            "installments": [dict(row) for row in self.installments],
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_plan_schedule(
    original_amount: Any,
    installments: Any,
    now: datetime | None = None,
    policy: dict[str, Any] | None = None,
) -> PlanVerdict:
    """Gate one customer-proposed installment schedule against merchant policy.

    The chatbot negotiates in prose and may propose anything; this function is
    the only place that decides whether a schedule is valid. Like ``evaluate()``
    it calls no LLM, sends nothing, creates no payment link and writes to no
    store — it returns a verdict plus every gate that was evaluated.

    ``installments`` is a sequence of ``{"amount": ..., "due_date": ...}``. Due
    dates may be blank (meaning today) or ISO dates; approved verdicts always
    carry them back as ``YYYY-MM-DD``.
    """
    rules = plan_policy() if policy is None else {**plan_policy(), **policy}
    today = (now or datetime.now(timezone.utc)).astimezone(IST).date()
    amount_due = _plan_money(original_amount)
    rows = _plan_rows(installments)
    checks: list[PolicyCheck] = []

    def verdict(decision: str, code: str, **params: Any) -> PlanVerdict:
        return PlanVerdict(
            decision=decision,
            reason_code=code,
            reason=describe_reason(code, **params),
            original_amount=amount_due,
            installments=tuple(dict(row) for row in rows),
            checks=tuple(checks),
        )

    # 1. The debt itself. Without it there is nothing to divide.
    checks.append(PolicyCheck("plan_amount_known", amount_due > 0, f"INR {amount_due:,.2f}"))
    if amount_due <= 0:
        return verdict("revise", "plan_amount_unknown")

    # 2. A schedule with no payable rows is not a schedule.
    checks.append(PolicyCheck("plan_not_empty", bool(rows), f"{len(rows)} payable installment(s)"))
    if not rows:
        return verdict("revise", "plan_empty")

    # 3. Maximum number of installments.
    max_count = int(rules["max_installments"])
    count_ok = len(rows) <= max_count
    checks.append(PolicyCheck("plan_installment_count", count_ok, f"{len(rows)} of maximum {max_count}"))
    if not count_ok:
        return verdict("revise", "plan_too_many_installments", count=len(rows), max_count=max_count)

    # 4. Whether splitting the debt at all is permitted on this account.
    partial_ok = bool(rules["partial_payment_allowed"]) or len(rows) == 1
    checks.append(
        PolicyCheck(
            "plan_partial_allowed",
            partial_ok,
            "part payments enabled" if partial_ok else "part payments disabled",
        )
    )
    if not partial_ok:
        return verdict("revise", "plan_partial_not_allowed")

    # 5. Minimum amounts. A single payment of the whole debt is exempt: nothing
    #    is being deferred, so a per-installment floor would be meaningless.
    #    The floor is scaled to this debt so that a small balance stays divisible
    #    instead of being refused by a threshold written for a large one.
    minimum = effective_min_installment(amount_due, rules)
    if len(rows) > 1:
        small = next((row for row in rows if row["amount"] < minimum - 0.01), None)
        checks.append(
            PolicyCheck(
                "plan_minimum_amounts",
                small is None,
                f"installment {small['index']} INR {small['amount']:,.2f}" if small else f"all at or above INR {minimum:,.0f}",
            )
        )
        if small is not None:
            return verdict(
                "revise",
                "plan_installment_too_small",
                index=small["index"],
                amount=f"INR {small['amount']:,.0f}",
                threshold=f"INR {minimum:,.0f}",
            )

        first_floor = min_first_payment(amount_due, rules)
        first_ok = rows[0]["amount"] >= first_floor - 0.01
        checks.append(PolicyCheck("plan_first_payment", first_ok, f"INR {rows[0]['amount']:,.2f} vs INR {first_floor:,.2f}"))
        if not first_ok:
            return verdict(
                "revise",
                "plan_first_payment_too_small",
                amount=f"INR {rows[0]['amount']:,.0f}",
                threshold=f"INR {first_floor:,.0f}",
            )

    # 6. Dates: readable, not in the past, strictly increasing, deferral allowed.
    allow_future = bool(rules["future_dates_allowed"])
    previous: date | None = None
    for row in rows:
        due = _plan_date(row["due_date"], today)
        if due is None:
            checks.append(PolicyCheck("plan_due_dates", False, f"installment {row['index']} '{row['due_date']}'"))
            return verdict("revise", "plan_invalid_date", index=row["index"], detail=row["due_date"] or "(blank)")
        row["due_date"] = due.isoformat()
        if due < today:
            checks.append(PolicyCheck("plan_due_dates", False, f"installment {row['index']} {row['due_date']} before {today.isoformat()}"))
            return verdict("revise", "plan_due_date_past", index=row["index"], detail=row["due_date"])
        if previous is not None and due <= previous:
            checks.append(PolicyCheck("plan_due_dates", False, f"installment {row['index']} {row['due_date']} not after {previous.isoformat()}"))
            return verdict("revise", "plan_dates_out_of_order", index=row["index"], detail=row["due_date"])
        if due > today and not allow_future:
            checks.append(PolicyCheck("plan_due_dates", False, "deferred dates disabled"))
            return verdict("revise", "plan_future_dates_not_allowed")
        previous = due
    checks.append(PolicyCheck("plan_due_dates", True, " → ".join(str(row["due_date"]) for row in rows)))

    # 7. Maximum extension period, measured to the final installment.
    max_days = int(rules["max_extension_days"])
    horizon = (previous - today).days if previous is not None else 0
    window_ok = horizon <= max_days
    checks.append(PolicyCheck("plan_extension_window", window_ok, f"{horizon}/{max_days} days"))
    if not window_ok:
        return verdict(
            "revise",
            "plan_extension_too_long",
            detail=rows[-1]["due_date"],
            days=horizon,
            max_days=max_days,
        )

    # 8. The schedule must add up to the debt. Discounts are a commercial
    #    decision and stay off the automated path unless explicitly enabled.
    discounts_allowed = bool(rules["discounts_allowed"])
    total = round(sum(float(row["amount"]) for row in rows), 2)
    short = total < amount_due - PLAN_TOTAL_TOLERANCE
    excess = total > amount_due + PLAN_TOTAL_TOLERANCE
    total_ok = not excess and (not short or discounts_allowed)
    checks.append(PolicyCheck("plan_total_matches_due", total_ok, f"INR {total:,.2f} vs INR {amount_due:,.2f}"))
    if excess:
        return verdict("revise", "plan_total_excess", total=f"INR {total:,.0f}", required=f"INR {amount_due:,.0f}")
    if short and not discounts_allowed:
        return verdict("revise", "plan_total_short", total=f"INR {total:,.0f}", required=f"INR {amount_due:,.0f}")

    return verdict("approve", "plan_approved", count=len(rows), total=f"INR {total:,.0f}")


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

        # --- Flexible-plan schedule gate ---------------------------------
        plan_cases: list[tuple[str, float, list[dict[str, Any]], str, str]] = [
            (
                "split plan inside policy approves",
                10000,
                [{"amount": 3000, "due_date": ""}, {"amount": 7000, "due_date": "2026-09-04"}],
                "approve",
                "plan_approved",
            ),
            (
                "full amount on a future date approves",
                10000,
                [{"amount": 10000, "due_date": "2026-09-10"}],
                "approve",
                "plan_approved",
            ),
            (
                "short total is not a discount",
                10000,
                [{"amount": 3000, "due_date": ""}, {"amount": 4000, "due_date": "2026-09-04"}],
                "revise",
                "plan_total_short",
            ),
            (
                "too many installments rejected",
                10000,
                [
                    {"amount": 2000, "due_date": ""},
                    {"amount": 2000, "due_date": "2026-09-04"},
                    {"amount": 3000, "due_date": "2026-09-10"},
                    {"amount": 3000, "due_date": "2026-09-15"},
                ],
                "revise",
                "plan_too_many_installments",
            ),
            (
                "first payment below the floor rejected",
                10000,
                [{"amount": 600, "due_date": ""}, {"amount": 9400, "due_date": "2026-09-06"}],
                "revise",
                "plan_first_payment_too_small",
            ),
            (
                "installment below the minimum rejected",
                10000,
                [{"amount": 9800, "due_date": ""}, {"amount": 200, "due_date": "2026-09-06"}],
                "revise",
                "plan_installment_too_small",
            ),
            (
                "extension beyond the window rejected",
                10000,
                [{"amount": 3000, "due_date": ""}, {"amount": 7000, "due_date": "2026-12-01"}],
                "revise",
                "plan_extension_too_long",
            ),
            (
                "past due date rejected",
                10000,
                [{"amount": 3000, "due_date": "2026-08-01"}, {"amount": 7000, "due_date": "2026-09-05"}],
                "revise",
                "plan_due_date_past",
            ),
            (
                "out of order dates rejected",
                10000,
                [{"amount": 3000, "due_date": "2026-09-08"}, {"amount": 7000, "due_date": "2026-09-05"}],
                "revise",
                "plan_dates_out_of_order",
            ),
            (
                "small debt splits into two reachable installments",
                199,
                [{"amount": 100, "due_date": ""}, {"amount": 99, "due_date": "2026-09-10"}],
                "approve",
                "plan_approved",
            ),
            (
                "small debt splits across the full installment allowance",
                199,
                [
                    {"amount": 67, "due_date": ""},
                    {"amount": 66, "due_date": "2026-09-10"},
                    {"amount": 66, "due_date": "2026-09-20"},
                ],
                "approve",
                "plan_approved",
            ),
            (
                "small debt still refuses a token installment",
                199,
                [{"amount": 50, "due_date": ""}, {"amount": 149, "due_date": "2026-09-10"}],
                "revise",
                "plan_installment_too_small",
            ),
            ("empty schedule rejected", 10000, [], "revise", "plan_empty"),
            (
                "unknown amount rejected",
                0,
                [{"amount": 3000, "due_date": ""}],
                "revise",
                "plan_amount_unknown",
            ),
        ]
        for label, due, schedule, expected_decision, expected_code in plan_cases:
            plan_result = evaluate_plan_schedule(due, schedule, now=daytime)
            ok = plan_result.decision == expected_decision and plan_result.reason_code == expected_code
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'} {label}: {plan_result.decision}/{plan_result.reason_code} — {plan_result.reason}")

        approved = evaluate_plan_schedule(
            10000,
            [{"amount": 3000, "due_date": ""}, {"amount": 7000, "due_date": "2026-09-04"}],
            now=daytime,
        )
        derived_ok = (
            approved.due_now == 3000.0
            and approved.remaining == 7000.0
            and approved.shortfall == 0.0
            and approved.installments[0]["due_date"] == "2026-09-01"
        )
        failures += 0 if derived_ok else 1
        print(f"{'PASS' if derived_ok else 'FAIL'} approved plan derives due now / remaining / resolved dates")

        # The floors must stay exactly as configured on a debt big enough to meet
        # them, and must never ask for the entire balance on one too small to.
        large_unchanged = (
            effective_min_installment(10000) == PLAN_MIN_INSTALLMENT_AMOUNT
            and min_first_payment(10000) == 2000.0
        )
        failures += 0 if large_unchanged else 1
        print(f"{'PASS' if large_unchanged else 'FAIL'} configured floors unchanged on a large debt")

        small_floor = min_first_payment(199)
        small_divisible = (
            0 < effective_min_installment(199) < 199
            and 0 < small_floor < 199
            and small_floor >= PLAN_ABSOLUTE_MIN_INSTALLMENT
        )
        failures += 0 if small_divisible else 1
        print(
            f"{'PASS' if small_divisible else 'FAIL'} small debt keeps a reachable first payment: "
            f"INR {small_floor:,.2f} of INR 199"
        )

        if failures:
            raise SystemExit(1)
