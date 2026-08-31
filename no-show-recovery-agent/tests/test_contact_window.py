"""The contact-window policy itself, held under test past the default clock.

``tests/conftest.py`` supplies an in-window default ``now`` so that behavioural
tests stop depending on the hour the suite happens to run. That default must not
become a way of switching the quiet-hour rule off, so these tests pass explicit
timestamps and assert the real decision on both sides of the boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone

import modules.attempt_tracker as attempt_tracker
import modules.policy_engine as policy_engine
from modules.attempt_tracker import is_contact_hour_allowed
from modules.policy_engine import (
    CONTACT_WINDOW_END_HOUR,
    CONTACT_WINDOW_START_HOUR,
    evaluate,
    next_contact_window_open,
)
from tests.conftest import IN_WINDOW_NOW, IST

# ``is_contact_hour_allowed`` above is bound at import time, so it stays the real
# predicate even while the autouse fixture replaces the module attribute. That is
# what the explicit-timestamp tests below want: the genuine rule, not the default.

PROPOSAL = {"recommended_intervention": "retry_payment", "confidence": 0.9, "root_cause": "soft decline"}


def _event(client_id: str = "WINDOW-1") -> dict:
    return {
        "event_type": "payment_failed",
        "client_id": client_id,
        "client_name": "Window Client",
        "client_email": "window@example.com",
        "invoice_id": "inv_window",
        "amount": 2400,
        "validation_errors": [],
        "source": "test",
    }


def _paths(tmp_path) -> dict:
    return {"attempts_path": tmp_path / "attempts.sqlite3", "decisions_path": tmp_path / "decisions.sqlite3"}


def test_an_explicit_night_timestamp_still_defers(tmp_path):
    """The fixture only fills in a missing clock; a supplied one is obeyed."""
    verdict = evaluate(_event(), PROPOSAL, now=datetime(2026, 9, 1, 23, 30, tzinfo=IST), **_paths(tmp_path))
    assert verdict.decision == "defer"
    assert verdict.reason_code == "outside_contact_window"
    assert verdict.contact_window_ok is False


def test_the_same_case_is_approved_inside_the_window(tmp_path):
    """Same case, same data, different hour — the hour is the only variable."""
    verdict = evaluate(_event("WINDOW-2"), PROPOSAL, now=datetime(2026, 9, 1, 11, 0, tzinfo=IST), **_paths(tmp_path))
    assert verdict.decision == "approve"
    assert verdict.contact_window_ok is True


def test_a_quiet_hour_hold_is_a_deferral_not_an_escalation(tmp_path):
    """A held case stays in the automated queue and names when it reopens."""
    night = datetime(2026, 9, 1, 2, 0, tzinfo=IST)
    verdict = evaluate(_event("WINDOW-3"), PROPOSAL, now=night, **_paths(tmp_path))
    assert verdict.deferred is True
    assert verdict.action == "retry_payment"
    assert verdict.next_attempt_at == next_contact_window_open(night)


def test_the_boundary_hours_are_inclusive_at_the_open_and_exclusive_at_the_close():
    """08:00 IST may contact; 22:00 IST may not."""
    assert is_contact_hour_allowed(datetime(2026, 9, 1, CONTACT_WINDOW_START_HOUR, 0, tzinfo=IST)) is True
    assert is_contact_hour_allowed(datetime(2026, 9, 1, CONTACT_WINDOW_END_HOUR, 0, tzinfo=IST)) is False


def test_the_window_is_judged_in_ist_not_in_utc():
    """17:30 UTC is 23:00 IST — quiet — which is exactly the case that made the
    suite fail when it ran on a machine reporting UTC afternoons."""
    assert is_contact_hour_allowed(datetime(2026, 8, 31, 17, 30, tzinfo=timezone.utc)) is False


def test_the_default_clock_supplied_by_the_fixture_is_inside_the_window():
    """Guards the fixture itself: if this default ever drifts out of the window,
    eight behavioural tests start failing again for a reason unrelated to them."""
    assert is_contact_hour_allowed(IN_WINDOW_NOW) is True


def test_both_module_bindings_are_redirected_to_the_default_clock():
    """``policy_engine`` imported the predicate by name, so patching only
    ``attempt_tracker`` would leave the engine reading the wall clock."""
    assert attempt_tracker.is_contact_hour_allowed() is True
    assert policy_engine.is_contact_hour_allowed() is True
