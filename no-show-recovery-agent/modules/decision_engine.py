"""Deterministic intervention rules for detected revenue-risk events."""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _is_first_offense(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def decide(event: dict[str, Any]) -> str:
    """Return the intervention action, escalating invalid event data explicitly."""
    if event.get("validation_errors"):
        return "escalate_human"
    event_type = event.get("event_type")
    if event_type in {"no_show", "calendar_cancellation"}:
        if _is_first_offense(event.get("is_first_offense", False)):
            return "friendly_reminder"
        urgency = _number(event.get("urgency_hours"))
        if urgency is None or urgency < 0:
            return "escalate_human"
        if urgency < 2:
            return "charge_fee"
        if event.get("waitlist_entry_exists") is True:
            return "offer_waitlist"
        return "escalate_human"
    if event_type == "failed_subscription":
        attempts = _non_negative_integer(event.get("attempt_count"))
        if attempts is None:
            return "escalate_human"
        return "retry_payment" if attempts < 3 else "escalate_human"
    return "escalate_human"


if __name__ == "__main__":
    cases = [
        ({"event_type": "no_show", "urgency_hours": 1.5, "is_first_offense": False}, "charge_fee"),
        ({"event_type": "no_show", "urgency_hours": 3, "waitlist_entry_exists": True, "is_first_offense": False}, "offer_waitlist"),
        ({"event_type": "no_show", "urgency_hours": 1, "is_first_offense": True}, "friendly_reminder"),
        ({"event_type": "failed_subscription", "attempt_count": 2}, "retry_payment"),
        ({"event_type": "failed_subscription", "attempt_count": 3}, "escalate_human"),
        ({"event_type": "unknown"}, "escalate_human"),
    ]
    for event, expected in cases:
        actual = decide(event)
        print(f"{actual}: {'PASS' if actual == expected else 'FAIL'} (expected {expected})")
    if any(decide(event) != expected for event, expected in cases):
        raise SystemExit(1)
