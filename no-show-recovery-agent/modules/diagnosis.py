"""Sandboxed LLM diagnosis layer. Proposes an intervention; never executes one.

Security / architecture contract
--------------------------------
This module is the **only** place an LLM touches the recovery pipeline's
decision path, and it has **no execution authority**:

* It receives a redacted view of a RevenueEvent (no email, no phone, no card
  data) — see :func:`redact_event`. That is the DPDP Act 2023 purpose-limitation
  posture: the model gets the minimum needed to classify a failure.
* It returns a strictly typed JSON proposal and nothing else. Free text outside
  the schema is discarded.
* It cannot set the money. ``amount`` is always taken from the event by the
  policy engine, never from model output, so a hallucinated figure cannot reach
  a payment link.
* It cannot invent an action. ``recommended_intervention`` is validated against
  :data:`ALLOWED_INTERVENTIONS` before it is returned to the caller, and the
  policy engine re-checks the allow-list independently.
* Any parse failure, schema violation, timeout, or missing API key falls back to
  :func:`heuristic_diagnosis` — a deterministic classifier. The batch therefore
  produces the same audit trail with or without a live model.

The proposal is advisory. ``modules.policy_engine.evaluate()`` decides what may
actually run.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable

# Kept in sync with the executor allow-list. The policy engine re-validates.
ALLOWED_INTERVENTIONS = (
    "retry_payment",
    "resend_payment_link",
    "charge_fee",
    "friendly_reminder",
    "firm_reminder",
    "final_notice",
    "offer_waitlist",
    "escalate_human",
)

ALLOWED_CHANNELS = ("email", "none")

ROOT_CAUSES = (
    "card_expired",
    "insufficient_funds",
    "issuer_declined",
    "bank_declined",
    "payment_method_unsupported",
    "late_cancellation",
    "first_time_cancellation",
    "advance_notice_cancellation",
    "checkout_abandoned",
    "unknown",
)

# --- Confidence calibration ------------------------------------------------
# These are the deterministic fallback's confidences. They were tuned down from
# a first pass that emitted 0.9 almost everywhere and auto-approved cases a
# human should have seen (see README "What broke"). Anything at or above
# policy_engine.CONFIDENCE_AUTO_APPROVE (0.75) auto-approves, so a value below
# that is a deliberate "a human should look at this".
CONF_UNAMBIGUOUS_SIGNAL = 0.88   # first offense, expired card: one obvious fix
CONF_STRONG_SIGNAL = 0.82        # clear late cancellation, clear funds issue
CONF_PROBABLE_SIGNAL = 0.78      # generic decline, retry is reasonable
CONF_AMBIGUOUS = 0.68            # mid-notice cancellation: fee justification unclear
CONF_WEAK = 0.55                 # bank-side decline, cause not attributable
CONF_UNKNOWN = 0.40             # unrecognised failure code: do not guess

SYSTEM_PROMPT = """You are a revenue-recovery diagnostician for an Indian B2B receivables system.

You classify why a payment or booking failed and propose ONE intervention.

Hard limits on your role:
- You have NO execution authority. You never send messages, never create payment
  links, never charge anyone. A separate deterministic policy engine decides
  whether your proposal runs at all.
- You never state an amount, a currency figure, an invoice number, or a customer
  contact detail. Those come from the system of record, not from you.
- You never assert that an action has happened. You propose only.
- If the evidence does not support a confident classification, say so with a low
  confidence score and recommend escalate_human. A low score is a correct
  answer, not a failure.

Reply with ONE JSON object and no other text, no markdown fence, no commentary:
{
  "root_cause": one of %(root_causes)s,
  "recommended_intervention": one of %(interventions)s,
  "confidence": number between 0.0 and 1.0,
  "reasoning": one sentence under 200 characters citing only the given fields,
  "channel": one of %(channels)s,
  "urgency": one of ["low", "medium", "high"]
}""" % {
    "root_causes": json.dumps(list(ROOT_CAUSES)),
    "interventions": json.dumps(list(ALLOWED_INTERVENTIONS)),
    "channels": json.dumps(list(ALLOWED_CHANNELS)),
}


class DiagnosisSchemaError(ValueError):
    """Raised when model output does not satisfy the typed contract."""


# ---------------------------------------------------------------------------
# Input redaction (DPDP purpose limitation)
# ---------------------------------------------------------------------------

# Fields the model is allowed to see. Everything else is withheld.
_VISIBLE_FIELDS = (
    "event_type",
    "decline_class",
    "source",
    "failure_reason",
    "error_code",
    "error_description",
    "attempt_count",
    "is_first_offense",
    "urgency_hours",
    "aging_bucket",
    "waitlist_entry_exists",
    "previous_failure_count",
    "promise_to_pay_date",
    "amount_band",
)


def amount_band(amount: Any) -> str:
    """Bucket an amount so the model sees scale without seeing the figure."""
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(value) or value <= 0:
        return "unknown"
    if value < 1000:
        return "small"
    if value < 10000:
        return "medium"
    if value < 50000:
        return "large"
    return "very_large"


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return the minimal, PII-free view of an event for the model."""
    for key in ("amount", "amount_at_risk", "invoice_amount", "appointment_value", "subscription_amount", "fee_amount"):
        value = event.get(key)
        if value:
            event = {**event, "amount_band": amount_band(value)}
            break
    return {key: event[key] for key in _VISIBLE_FIELDS if key in event and event[key] not in (None, "")}


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply, tolerating fences."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    match = _JSON_OBJECT.search(text)
    if not match:
        raise DiagnosisSchemaError("no JSON object in model output")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DiagnosisSchemaError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DiagnosisSchemaError("model output is not a JSON object")
    return parsed


def validate_diagnosis(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce and validate a proposal against the typed contract.

    Raises :class:`DiagnosisSchemaError` on any violation. Extra keys the model
    invented — including any amount it tried to assert — are dropped.
    """
    if not isinstance(payload, dict):
        raise DiagnosisSchemaError("proposal must be an object")

    intervention = str(payload.get("recommended_intervention") or "").strip().lower()
    if intervention not in ALLOWED_INTERVENTIONS:
        raise DiagnosisSchemaError(f"recommended_intervention '{intervention}' not in allow-list")

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise DiagnosisSchemaError("confidence must be a number") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise DiagnosisSchemaError("confidence must be between 0.0 and 1.0")

    root_cause = str(payload.get("root_cause") or "").strip().lower()
    if root_cause not in ROOT_CAUSES:
        raise DiagnosisSchemaError(f"root_cause '{root_cause}' not in taxonomy")

    channel = str(payload.get("channel") or "email").strip().lower()
    if channel not in ALLOWED_CHANNELS:
        raise DiagnosisSchemaError(f"channel '{channel}' not supported")

    urgency = str(payload.get("urgency") or "medium").strip().lower()
    if urgency not in ("low", "medium", "high"):
        raise DiagnosisSchemaError(f"urgency '{urgency}' not recognised")

    reasoning = str(payload.get("reasoning") or "").strip()
    if not reasoning:
        raise DiagnosisSchemaError("reasoning is required")
    # Strip any currency figure the model tried to assert. Amounts come from the
    # system of record only.
    reasoning = re.sub(r"(?:₹|\bINR\b|\bRs\.?\b)\s*[\d,]+(?:\.\d+)?", "[amount withheld]", reasoning, flags=re.IGNORECASE)

    return {
        "root_cause": root_cause,
        "recommended_intervention": intervention,
        "confidence": round(confidence, 4),
        "reasoning": reasoning[:200],
        "channel": channel,
        "urgency": urgency,
    }


# ---------------------------------------------------------------------------
# Deterministic fallback classifier
# ---------------------------------------------------------------------------

_FAILURE_MAP: dict[str, tuple[str, str, float, str]] = {
    # failure_reason -> (root_cause, intervention, confidence, urgency)
    "card_expired": ("card_expired", "resend_payment_link", CONF_UNAMBIGUOUS_SIGNAL, "medium"),
    "expired_card": ("card_expired", "resend_payment_link", CONF_UNAMBIGUOUS_SIGNAL, "medium"),
    "insufficient_funds": ("insufficient_funds", "retry_payment", CONF_STRONG_SIGNAL, "medium"),
    "card_declined": ("issuer_declined", "retry_payment", CONF_PROBABLE_SIGNAL, "medium"),
    "bank_declined": ("bank_declined", "escalate_human", CONF_WEAK, "high"),
    "payment_method_failed": ("payment_method_unsupported", "escalate_human", CONF_UNKNOWN, "high"),
}


def _first_offense(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _notice_hours(event: dict[str, Any]) -> float | None:
    try:
        hours = float(event.get("urgency_hours"))
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) and hours >= 0 else None


def heuristic_diagnosis(event: dict[str, Any]) -> dict[str, Any]:
    """Classify an event without a model. Same schema, deterministic output."""
    event_type = str(event.get("event_type") or "").strip().lower()

    if event_type in {"failed_subscription", "payment_failed", "invoice_overdue", "subscription_halted"}:
        reason = str(event.get("failure_reason") or event.get("error_code") or "").strip().lower()
        decline_class = str(event.get("decline_class") or "unknown").strip().lower()
        root_cause, intervention, confidence, urgency = _FAILURE_MAP.get(
            reason, ("unknown", "escalate_human", CONF_UNKNOWN, "high")
        )
        # Ingestion classification is authoritative for the broad action branch.
        # It prevents a novel hard-decline code from becoming a blind retry.
        if decline_class == "hard" and intervention == "escalate_human":
            root_cause, intervention, confidence, urgency = "payment_method_unsupported", "resend_payment_link", CONF_STRONG_SIGNAL, "medium"
        elif decline_class == "soft" and intervention == "escalate_human":
            root_cause, intervention, confidence, urgency = "issuer_declined", "retry_payment", CONF_PROBABLE_SIGNAL, "medium"
        elif event_type == "failed_subscription" and decline_class == "unknown" and not reason:
            # Legacy CSV rows predate gateway decline enrichment. Preserve the
            # existing recovery playbook while real webhook ambiguity remains a
            # human-review case.
            root_cause, intervention, confidence, urgency = "legacy_unclassified_decline", "retry_payment", CONF_STRONG_SIGNAL, "medium"
        reasoning = (
            f"Gateway reported '{reason or 'no failure code'}'; mapped to {root_cause} by the deterministic classifier."
        )
        return {
            "root_cause": root_cause,
            "recommended_intervention": intervention,
            "confidence": confidence,
            "reasoning": reasoning[:200],
            "channel": "none" if intervention == "escalate_human" else "email",
            "urgency": urgency,
        }

    if event_type in {"no_show", "calendar_cancellation", "checkout_abandoned"}:
        if event_type == "checkout_abandoned":
            return {
                "root_cause": "checkout_abandoned",
                "recommended_intervention": "resend_payment_link",
                "confidence": CONF_STRONG_SIGNAL,
                "reasoning": "Checkout started but never completed; resending the link is the lowest-friction recovery.",
                "channel": "email",
                "urgency": "medium",
            }
        if _first_offense(event.get("is_first_offense", False)):
            return {
                "root_cause": "first_time_cancellation",
                "recommended_intervention": "friendly_reminder",
                "confidence": CONF_UNAMBIGUOUS_SIGNAL,
                "reasoning": "First recorded cancellation for this client; goodwill rebooking beats a fee.",
                "channel": "email",
                "urgency": "low",
            }
        notice = _notice_hours(event)
        if notice is None:
            return {
                "root_cause": "unknown",
                "recommended_intervention": "escalate_human",
                "confidence": CONF_UNKNOWN,
                "reasoning": "Cancellation timing is missing, so notice period cannot be established.",
                "channel": "none",
                "urgency": "high",
            }
        if notice < 2:
            return {
                "root_cause": "late_cancellation",
                "recommended_intervention": "charge_fee",
                "confidence": CONF_STRONG_SIGNAL,
                "reasoning": f"Repeat cancellation with only {notice:.1f}h notice; the slot could not be refilled.",
                "channel": "email",
                "urgency": "high",
            }
        if event.get("waitlist_entry_exists") is True:
            return {
                "root_cause": "advance_notice_cancellation",
                "recommended_intervention": "offer_waitlist",
                "confidence": CONF_STRONG_SIGNAL,
                "reasoning": f"{notice:.1f}h notice with a waitlist entry available; refill the slot instead of charging.",
                "channel": "email",
                "urgency": "medium",
            }
        # Mid and long notice without a waitlist: whether a fee is defensible is
        # a judgement call, so this deliberately lands under the auto-approve
        # bar and routes to a human with the reason visible.
        return {
            "root_cause": "advance_notice_cancellation",
            "recommended_intervention": "charge_fee" if notice < 12 else "friendly_reminder",
            "confidence": CONF_AMBIGUOUS,
            "reasoning": f"{notice:.1f}h notice and no waitlist entry; fee justification is a judgement call.",
            "channel": "email",
            "urgency": "medium",
        }

    return {
        "root_cause": "unknown",
        "recommended_intervention": "escalate_human",
        "confidence": CONF_UNKNOWN,
        "reasoning": f"Event type '{event_type or 'missing'}' has no classification rule.",
        "channel": "none",
        "urgency": "high",
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def diagnose(
    event: dict[str, Any],
    llm: Callable[[str], str] | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Return a validated, typed proposal for one event.

    ``source`` records how the proposal was produced (``llm``,
    ``llm_fallback_heuristic``, or ``heuristic``) so the audit trail always
    shows whether a model was involved. ``schema_error`` is populated when a
    model reply was rejected, which is what makes the sandbox observable.
    """
    if event.get("validation_errors"):
        # Do not spend a model call on data we already know is unusable.
        return {
            "root_cause": "unknown",
            "recommended_intervention": "escalate_human",
            "confidence": 0.0,
            "reasoning": "Event failed ingestion validation; not diagnosable.",
            "channel": "none",
            "urgency": "high",
            "source": "skipped_invalid_event",
            "schema_error": None,
        }

    if use_llm or llm is not None:
        caller = llm
        if caller is None:
            from modules.message_generator import call_llm as caller  # lazy: keeps offline runs import-light
        prompt = f"{SYSTEM_PROMPT}\n\nEvent:\n{json.dumps(redact_event(event), sort_keys=True)}"
        try:
            proposal = validate_diagnosis(_extract_json(caller(prompt)))
            proposal["source"] = "llm"
            proposal["schema_error"] = None
            return proposal
        except (DiagnosisSchemaError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            fallback = heuristic_diagnosis(event)
            fallback["source"] = "llm_fallback_heuristic"
            fallback["schema_error"] = str(exc)[:200]
            return fallback

    proposal = heuristic_diagnosis(event)
    proposal["source"] = "heuristic"
    proposal["schema_error"] = None
    return proposal


if __name__ == "__main__":
    checks: list[tuple[str, bool]] = []

    # Schema gate rejects hallucinated capability and hallucinated money.
    for bad, label in [
        ('{"root_cause":"card_expired","recommended_intervention":"wire_funds","confidence":0.9,"reasoning":"x","channel":"email","urgency":"low"}', "off-menu action rejected"),
        ('{"root_cause":"card_expired","recommended_intervention":"retry_payment","confidence":4,"reasoning":"x","channel":"email","urgency":"low"}', "out-of-range confidence rejected"),
        ('{"root_cause":"aliens","recommended_intervention":"retry_payment","confidence":0.9,"reasoning":"x","channel":"email","urgency":"low"}', "unknown root cause rejected"),
        ("I think you should just retry it.", "non-JSON rejected"),
    ]:
        try:
            validate_diagnosis(_extract_json(bad))
            checks.append((label, False))
        except DiagnosisSchemaError:
            checks.append((label, True))

    stripped = validate_diagnosis(
        {
            "root_cause": "card_expired",
            "recommended_intervention": "retry_payment",
            "confidence": 0.9,
            "reasoning": "Customer owes ₹48,000 on this invoice.",
            "channel": "email",
            "urgency": "low",
            "amount": 48000,
        }
    )
    checks.append(("model-asserted amount stripped", "amount" not in stripped and "48,000" not in stripped["reasoning"]))

    redacted = redact_event({"client_email": "a@b.com", "client_phone": "+919999999999", "event_type": "payment_failed", "failure_reason": "card_expired", "subscription_amount": 1299})
    checks.append(("PII withheld from model input", "client_email" not in redacted and "client_phone" not in redacted and redacted["amount_band"] == "medium"))

    checks.append(("expired card proposes link resend", heuristic_diagnosis({"event_type": "failed_subscription", "failure_reason": "card_expired"})["recommended_intervention"] == "resend_payment_link"))
    checks.append(("unknown failure code escalates", heuristic_diagnosis({"event_type": "failed_subscription", "failure_reason": "gremlins"})["recommended_intervention"] == "escalate_human"))
    checks.append(("late cancellation charges fee", heuristic_diagnosis({"event_type": "no_show", "is_first_offense": False, "urgency_hours": 1.2})["recommended_intervention"] == "charge_fee"))
    checks.append(("mid-notice lands below auto-approve bar", heuristic_diagnosis({"event_type": "no_show", "is_first_offense": False, "urgency_hours": 6})["confidence"] < 0.75))

    broken = diagnose({"event_type": "failed_subscription", "failure_reason": "card_expired"}, llm=lambda _prompt: "not json at all")
    checks.append(("broken model reply falls back deterministically", broken["source"] == "llm_fallback_heuristic" and broken["schema_error"] is not None))

    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {label}")
    if not all(ok for _label, ok in checks):
        raise SystemExit(1)
