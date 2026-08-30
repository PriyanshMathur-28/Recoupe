"""The single canonical RevenueEvent schema plus enrichment.

Every ingestion source — the synthetic CSV, Google Calendar, and Razorpay
test-mode webhooks — is flattened into one shape here so that the diagnosis,
policy, executor, and audit layers only ever see one contract. Adding a source
means adding a mapper in this module and nothing else.

Layer 1 (ingestion) and layer 2 (enrichment) of the architecture live here.

Schema
------
Identity      event_id, event_type, source, occurred_at, detected_at
Customer      client_id, client_name, client_email
Money         amount, currency
Failure       failure_reason, error_code, error_description
History       attempt_count, previous_failure_count, aging_days, aging_bucket,
              promise_to_pay_date, opt_out
References    invoice_id, payment_id, subscription_id, payment_link_id
Integrity     validation_errors

The original source record is preserved under ``raw`` for the audit trail.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

# Canonical revenue-risk event types. These are what the rest of the pipeline
# branches on; the Razorpay event name is kept in ``raw`` for traceability.
EVENT_TYPES = (
    "payment_failed",        # payment.failed / subscription.charged.failed
    "subscription_pending",  # subscription.pending
    "subscription_halted",   # subscription.halted
    "invoice_partially_paid",  # invoice.partially_paid
    "invoice_expired",       # invoice.expired
    "payment_link_expired",  # payment_link.expired — treated as abandonment
    "checkout_abandoned",
    "failed_subscription",   # CSV synthetic subscription failure
    "no_show",               # CSV / calendar appointment loss
    "calendar_cancellation",
    "source_error",
)

# Razorpay webhook event name -> (canonical event type, payload entity key)
RAZORPAY_FAILURE_EVENTS: dict[str, tuple[str, str]] = {
    "payment.failed": ("payment_failed", "payment"),
    "subscription.charged.failed": ("payment_failed", "payment"),
    "subscription.pending": ("subscription_pending", "subscription"),
    "subscription.halted": ("subscription_halted", "subscription"),
    "invoice.partially_paid": ("invoice_partially_paid", "invoice"),
    "invoice.expired": ("invoice_expired", "invoice"),
    "payment_link.expired": ("payment_link_expired", "payment_link"),
}

# Invoice aging buckets drive the staged intervention ladder.
AGING_BUCKETS = ("current", "1-7", "8-30", "31-60", "60+")

# Gateway signals are classified before diagnosis. Soft declines can enter the
# retry ladder; hard declines must use a payment-method update link and are
# never blindly retried. Unknown signals fail closed to human review.
SOFT_DECLINE_SIGNALS = frozenset({
    "insufficient_funds", "issuer_declined", "bank_declined", "payment_timed_out",
    "gateway_error", "server_error", "temporarily_unavailable",
})
HARD_DECLINE_SIGNALS = frozenset({
    "card_expired", "expired_card", "invalid_card", "incorrect_card_details",
    "payment_method_unsupported", "authentication_failed", "card_not_supported",
})

_AMOUNT_KEYS = (
    "amount",
    "amount_at_risk",
    "invoice_amount",
    "fee_amount",
    "subscription_amount",
    "appointment_value",
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _positive_amount(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None
    return round(amount, 2)


def _paise_to_inr(value: Any) -> float | None:
    amount = _positive_amount(value)
    return None if amount is None else round(amount / 100, 2)


def _epoch_to_iso(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _iso_or_blank(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def aging_bucket(days: float | None) -> str:
    """Bucket invoice age in days for the staged intervention ladder."""
    if days is None or days < 0:
        return "current"
    if days <= 0:
        return "current"
    if days <= 7:
        return "1-7"
    if days <= 30:
        return "8-30"
    if days <= 60:
        return "31-60"
    return "60+"


def aging_days(occurred_at: str, now: datetime | None = None) -> float | None:
    """Return whole days between the failure and now, or None if unknown."""
    text = _clean(occurred_at)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0.0, round((reference - moment).total_seconds() / 86400, 2))


def build_event_id(event_type: str, client_id: str, reference: str) -> str:
    """Return a stable synthetic event id for sources that do not supply one."""
    digest = hashlib.sha256(
        "|".join([_clean(event_type), _clean(client_id).lower(), _clean(reference)]).encode("utf-8")
    ).hexdigest()
    return f"rev_{digest[:20]}"


def classify_decline(*values: Any) -> str:
    """Return ``soft``, ``hard``, or ``unknown`` from normalized gateway text."""
    signal = " ".join(_clean(value).lower() for value in values if _clean(value))
    normalized = signal.replace("-", "_").replace(" ", "_")
    if any(item in normalized for item in HARD_DECLINE_SIGNALS):
        return "hard"
    if any(item in normalized for item in SOFT_DECLINE_SIGNALS):
        return "soft"
    return "unknown"


def blank_event() -> dict[str, Any]:
    """Return the canonical schema with every field present and empty."""
    return {
        "event_id": "",
        "event_type": "",
        "source": "",
        "occurred_at": "",
        "detected_at": "",
        "client_id": None,
        "client_name": "",
        "client_email": "",
        "amount": None,
        "currency": "INR",
        "failure_reason": "",
        "error_code": "",
        "error_description": "",
        "decline_class": "unknown",
        "attempt_count": 0,
        "previous_failure_count": 0,
        "aging_days": None,
        "aging_bucket": "current",
        "promise_to_pay_date": "",
        "opt_out": False,
        "invoice_id": "",
        "payment_id": "",
        "subscription_id": "",
        "payment_link_id": "",
        "validation_errors": [],
        "raw": {},
    }


def _finalize(event: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Fill derived fields, run integrity checks, and return the event."""
    errors = [str(item) for item in (event.get("validation_errors") or [])]

    if not _clean(event.get("client_id")):
        event["client_id"] = None
        if "missing client_id" not in errors:
            errors.append("missing client_id")

    if event.get("event_type") not in EVENT_TYPES:
        errors.append(f"unsupported event_type '{_clean(event.get('event_type')) or 'missing'}'")

    amount_required_types = {
        "failed_subscription", "payment_failed", "invoice_overdue",
        "invoice_partially_paid", "payment_link_expired", "checkout_abandoned",
    }
    if event.get("event_type") in amount_required_types and _positive_amount(event.get("amount")) is None:
        errors.append("missing or non-positive amount")

    email = _clean(event.get("client_email"))
    if email and "@" not in email:
        errors.append("invalid client_email")
        event["client_email"] = ""

    event["decline_class"] = classify_decline(
        event.get("failure_reason"), event.get("error_code"), event.get("error_description")
    )

    if not event.get("detected_at"):
        event["detected_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    days = event.get("aging_days")
    if days is None:
        days = aging_days(event.get("occurred_at") or "", now)
        event["aging_days"] = days
    event["aging_bucket"] = aging_bucket(days)

    if not event.get("event_id"):
        event["event_id"] = build_event_id(
            event.get("event_type") or "",
            event.get("client_id") or "unknown",
            event.get("invoice_id") or event.get("payment_id") or event.get("subscription_id") or event.get("payment_link_id") or event.get("occurred_at") or "",
        )

    event["validation_errors"] = errors
    return event


# ---------------------------------------------------------------------------
# Source mappers
# ---------------------------------------------------------------------------

def from_detector_event(detected: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Promote a legacy detector event (CSV / Calendar) to a RevenueEvent.

    The legacy event fields are kept alongside the canonical ones so existing
    message templates and dashboard columns keep working during the migration.
    """
    event = blank_event()
    legacy_type = _clean(detected.get("event_type"))
    event["event_type"] = legacy_type if legacy_type in EVENT_TYPES else "source_error"
    event["source"] = _clean(detected.get("source")) or "recovery_cases.csv"
    event["client_id"] = detected.get("client_id")
    event["client_name"] = _clean(detected.get("client_name"))
    event["client_email"] = _clean(detected.get("client_email"))
    event["failure_reason"] = _clean(detected.get("failure_reason"))
    event["error_code"] = _clean(detected.get("failure_reason"))

    for key in _AMOUNT_KEYS:
        amount = _positive_amount(detected.get(key))
        if amount is not None:
            event["amount"] = amount
            break

    attempts = _non_negative_int(detected.get("attempt_count"))
    if attempts is None and legacy_type == "failed_subscription" and detected.get("attempt_count") is not None:
        event["validation_errors"] = list(detected.get("validation_errors") or [])
    event["attempt_count"] = attempts or 0
    event["previous_failure_count"] = attempts or 0

    # Recovery aging starts at the failed charge. Appointment timestamps belong
    # to a separate legacy vertical and must not trigger payment retry cooldowns.
    occurred = _iso_or_blank(
        detected.get("last_charge_date")
        or detected.get("cancellation_time")
    )
    event["occurred_at"] = occurred
    event["subscription_id"] = _clean(detected.get("subscription_id"))
    event["invoice_id"] = _clean(detected.get("invoice_id"))
    event["promise_to_pay_date"] = _iso_or_blank(detected.get("promise_to_pay_date"))
    event["opt_out"] = bool(detected.get("opt_out"))
    event["validation_errors"] = list(detected.get("validation_errors") or []) + list(event.get("validation_errors") or [])
    event["raw"] = {key: value for key, value in detected.items() if key != "raw"}

    finalized = _finalize(event, now)
    # Preserve legacy keys the decision/message layers still read.
    passthrough = {
        key: detected[key]
        for key in (
            "appointment_datetime",
            "appointment_value",
            "cancellation_time",
            "urgency_hours",
            "urgency_policy",
            "is_first_offense",
            "subscription_amount",
            "last_charge_date",
            "waitlist_entry_exists",
            "fee_amount",
            "short_url",
        )
        if key in detected
    }
    return {**finalized, **passthrough, "validation_errors": finalized["validation_errors"]}


def from_razorpay_webhook(payload: dict[str, Any], event_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Map a verified Razorpay failure webhook to a RevenueEvent.

    Handles ``payment.failed``, ``subscription.charged.failed``, ``subscription.pending``, ``subscription.halted``,
    ``invoice.partially_paid``, ``invoice.expired`` and ``payment_link.expired``.
    Successful-payment webhooks are handled separately by
    :mod:`modules.razorpay_webhooks` because they close a loop rather than open one.
    """
    event_name = _clean(payload.get("event"))
    if event_name not in RAZORPAY_FAILURE_EVENTS:
        raise ValueError(f"Unsupported Razorpay revenue event: {event_name or 'missing event'}")
    if not _clean(event_id):
        raise ValueError("Razorpay webhook event_id is required")

    event_type, entity_key = RAZORPAY_FAILURE_EVENTS[event_name]
    container = payload.get("payload") or {}
    entity = (container.get(entity_key) or {}).get("entity") or {}
    if not isinstance(entity, dict):
        raise ValueError(f"Razorpay webhook {entity_key} entity is missing")

    # Razorpay nests the customer differently per entity; check every known spot.
    customer = entity.get("customer") or (container.get("customer") or {}).get("entity") or {}
    notes = entity.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}

    event = blank_event()
    event["event_id"] = _clean(event_id)
    event["event_type"] = event_type
    event["source"] = "razorpay_webhook"
    event["client_id"] = _clean(notes.get("client_id")) or _clean(customer.get("email")) or _clean(entity.get("customer_id")) or None
    event["client_name"] = _clean(customer.get("name")) or _clean(notes.get("client_name"))
    event["client_email"] = _clean(customer.get("email")) or _clean(notes.get("client_email"))
    event["currency"] = _clean(entity.get("currency")) or "INR"

    if event_type == "invoice_partially_paid":
        total = _paise_to_inr(entity.get("amount")) or 0.0
        paid = _paise_to_inr(entity.get("amount_paid")) or 0.0
        event["amount"] = round(max(total - paid, 0.0), 2) or None
    else:
        event["amount"] = _paise_to_inr(entity.get("amount_due")) or _paise_to_inr(entity.get("amount"))

    event["error_code"] = _clean(entity.get("error_code"))
    event["error_description"] = _clean(entity.get("error_description"))
    event["failure_reason"] = _clean(entity.get("error_reason")) or event["error_code"] or event_name

    attempts = _non_negative_int(entity.get("attempts") or entity.get("paid_count") or notes.get("attempt_count"))
    event["attempt_count"] = attempts or 0
    event["previous_failure_count"] = attempts or 0

    event["occurred_at"] = (
        _epoch_to_iso(entity.get("created_at"))
        or _epoch_to_iso(payload.get("created_at"))
        or _iso_or_blank(notes.get("occurred_at"))
    )
    event["invoice_id"] = _clean(entity.get("invoice_id")) or (_clean(entity.get("id")) if entity_key == "invoice" else "")
    event["payment_id"] = _clean(entity.get("id")) if entity_key == "payment" else _clean(entity.get("payment_id"))
    subscription_entity = (container.get("subscription") or {}).get("entity") or {}
    event["subscription_id"] = (
        _clean(entity.get("subscription_id"))
        or _clean(subscription_entity.get("id"))
        or (_clean(entity.get("id")) if entity_key == "subscription" else "")
    )
    event["payment_link_id"] = _clean(entity.get("id")) if entity_key == "payment_link" else _clean(entity.get("payment_link_id"))
    event["promise_to_pay_date"] = _iso_or_blank(notes.get("promise_to_pay_date"))
    event["opt_out"] = _clean(notes.get("opt_out")).lower() in {"true", "1", "yes"}
    event["raw"] = {"event": event_name, "entity_key": entity_key, "entity": entity}

    return _finalize(event, now)


def enrich(event: dict[str, Any], history: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Layer 2: attach cross-event history to a normalized event.

    ``history`` is a per-client record, e.g.
    ``{"previous_failure_count": 2, "promise_to_pay_date": "...", "opt_out": True}``.
    Enrichment never overwrites a value the source already supplied with a
    weaker one; it only fills gaps and takes the larger failure count.
    """
    enriched = dict(event)
    record = history or {}

    prior = _non_negative_int(record.get("previous_failure_count"))
    if prior is not None:
        enriched["previous_failure_count"] = max(int(enriched.get("previous_failure_count") or 0), prior)

    if not _clean(enriched.get("promise_to_pay_date")):
        enriched["promise_to_pay_date"] = _iso_or_blank(record.get("promise_to_pay_date"))

    if record.get("opt_out"):
        enriched["opt_out"] = True

    if enriched.get("aging_days") is None:
        enriched["aging_days"] = aging_days(enriched.get("occurred_at") or "", now)
    enriched["aging_bucket"] = aging_bucket(enriched.get("aging_days"))
    return enriched


if __name__ == "__main__":
    checks: list[tuple[str, bool]] = []
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    failed = from_razorpay_webhook(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_TEST1",
                        "amount": 129900,
                        "currency": "INR",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "card expired",
                        "created_at": 1757000000,
                        "notes": {"client_id": "SUB004"},
                        "customer": {"name": "Ananya Iyer", "email": "ananya.iyer@example.com"},
                    }
                }
            },
        },
        event_id="evt_1",
        now=now,
    )
    checks.append(("payment.failed maps to canonical type", failed["event_type"] == "payment_failed"))
    checks.append(("paise converted to INR", failed["amount"] == 1299.0))
    checks.append(("no validation errors on a good payload", failed["validation_errors"] == []))
    checks.append(("aging bucket derived", failed["aging_bucket"] in AGING_BUCKETS))

    halted = from_razorpay_webhook(
        {
            "event": "subscription.halted",
            "payload": {"subscription": {"entity": {"id": "sub_1", "notes": {"client_id": "SUB010"}, "created_at": 1757000000}}},
        },
        event_id="evt_2",
        now=now,
    )
    checks.append(("subscription.halted supported", halted["event_type"] == "subscription_halted"))
    checks.append(("missing amount is flagged, not crashed", "missing or non-positive amount" in halted["validation_errors"]))

    partial = from_razorpay_webhook(
        {
            "event": "invoice.partially_paid",
            "payload": {"invoice": {"entity": {"id": "inv_9", "amount": 500000, "amount_paid": 200000, "notes": {"client_id": "C9"}, "created_at": 1757000000}}},
        },
        event_id="evt_3",
        now=now,
    )
    checks.append(("partial payment leaves the shortfall at risk", partial["amount"] == 3000.0))

    expired_link = from_razorpay_webhook(
        {"event": "payment_link.expired", "payload": {"payment_link": {"entity": {"id": "plink_1", "amount": 90000, "notes": {"client_id": "C10"}, "created_at": 1757000000}}}},
        event_id="evt_4",
        now=now,
    )
    checks.append(("payment_link.expired supported", expired_link["event_type"] == "payment_link_expired" and expired_link["payment_link_id"] == "plink_1"))

    try:
        from_razorpay_webhook({"event": "payment.captured", "payload": {}}, event_id="evt_5")
        checks.append(("success webhook rejected by this mapper", False))
    except ValueError:
        checks.append(("success webhook rejected by this mapper", True))

    legacy = from_detector_event(
        {
            "event_type": "failed_subscription",
            "client_id": "SUB003",
            "client_name": "Rohan Mehta",
            "client_email": "rohan.mehta@example.com",
            "subscription_amount": 999.0,
            "failure_reason": "card_declined",
            "attempt_count": 2,
            "last_charge_date": "2026-09-01",
            "validation_errors": [],
            "source": "recovery_cases.csv",
        },
        now=now,
    )
    checks.append(("legacy CSV event normalizes", legacy["event_type"] == "failed_subscription" and legacy["amount"] == 999.0))
    checks.append(("legacy keys preserved for templates", legacy["subscription_amount"] == 999.0))
    checks.append(("aging computed from last charge", legacy["aging_bucket"] == "8-30"))

    enriched = enrich(legacy, {"previous_failure_count": 5, "opt_out": True}, now=now)
    checks.append(("enrichment raises failure count and opt-out", enriched["previous_failure_count"] == 5 and enriched["opt_out"] is True))

    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {label}")
    if not all(ok for _label, ok in checks):
        raise SystemExit(1)
