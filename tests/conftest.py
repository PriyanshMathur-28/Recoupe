"""Shared test fixtures.

Why this file exists
--------------------
The recovery pipeline enforces a self-imposed contact window (08:00-22:00 IST,
``is_contact_hour_allowed``). Outside it, a payment or outreach action is
correctly *deferred* rather than executed — a deferral, not an escalation.

Most behavioural tests assert on the action a case reaches (``charge_fee``,
``escalate_human``, ``client_notified``) without pinning a clock, so they only
passed when the developer's machine happened to sit inside business hours. Run
the same suite at 23:00 IST and eight of them failed on the deferral, which
looks like a product regression and is not one.

The fixture below supplies a fixed in-window ``now`` *only when a caller passed
none*. Any test that deliberately exercises quiet hours still passes its own
timestamp and still sees the real decision, so the policy remains under test.
"""
from datetime import datetime, timedelta, timezone

import pytest

import modules.attempt_tracker as attempt_tracker
import modules.policy_engine as policy_engine

IST = timezone(timedelta(hours=5, minutes=30))

#: A weekday mid-morning IST — comfortably inside the contact window.
IN_WINDOW_NOW = datetime(2026, 9, 1, 11, 0, tzinfo=IST)

@pytest.fixture(autouse=True)
def default_clock_inside_contact_window(monkeypatch):
    """Make the suite's default 'now' land inside the contact window.

    ``policy_engine`` imported the predicate by name, so both module bindings
    have to be replaced or the engine keeps consulting the wall clock.
    """
    real = attempt_tracker.is_contact_hour_allowed

    def is_contact_hour_allowed(now: datetime | None = None) -> bool:
        return real(IN_WINDOW_NOW if now is None else now)

    monkeypatch.setattr(attempt_tracker, "is_contact_hour_allowed", is_contact_hour_allowed)
    monkeypatch.setattr(policy_engine, "is_contact_hour_allowed", is_contact_hour_allowed)
