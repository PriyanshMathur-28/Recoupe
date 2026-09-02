"""Flexible payment plan: policy gate, negotiation, billing, and webhook credit.

These tests cover the one feature path that spans every layer of the system —
voice call → plan invite → chatbot negotiation → policy gate → Razorpay
installment link → webhook credit → original recovery case — and they assert the
two properties that make it safe:

* The customer never approves their own schedule. ``evaluate_plan_schedule`` is
  the only approver, and ``negotiate`` may only offer the Confirm button when
  that gate said yes.
* An installment payment lands on the ORIGINAL case. The credit shares the
  recovery record's ``client_id``, so a plan payment never opens a second case,
  and a redelivered webhook cannot inflate the recovered total.

Every store is repointed at ``tmp_path``, and no test needs an LLM: with no
provider key configured the extraction layers fall back to their deterministic
heuristics, which is exactly the offline path CI runs.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules import flexible_plans, messenger, payments, plan_chat, plan_outreach, razorpay_webhooks
from modules.audit_log import read_events
from modules.policy_engine import evaluate_plan_schedule

CASE = {
    "client_id": "CASE_1",
    "client_name": "Aditya",
    "client_email": "aditya@example.com",
    "amount": 10000.0,
    "case_key": "CASE_1:retry_payment",
    "case": {
        "client_id": "CASE_1",
        "client_name": "Aditya",
        "client_email": "aditya@example.com",
        "event_type": "payment_failed",
        "amount": 10000.0,
        "currency": "INR",
    },
}


@pytest.fixture()
def plan_db(tmp_path: Path) -> Path:
    return tmp_path / "plans.sqlite3"


def _open_plan(plan_db: Path) -> tuple[dict, str]:
    return flexible_plans.create_or_refresh_plan(CASE, voice_hint="I can pay Rs 3,000 today.", path=plan_db)


# --------------------------------------------------------------------------- #
# The policy gate is the only approver
# --------------------------------------------------------------------------- #

def test_schedule_inside_policy_is_approved():
    now = datetime.now(timezone.utc)
    verdict = evaluate_plan_schedule(
        10000.0,
        [
            {"amount": 3000, "due_date": now.date().isoformat()},
            {"amount": 7000, "due_date": (now + timedelta(days=3)).date().isoformat()},
        ],
        now=now,
    )
    assert verdict.approved is True
    assert verdict.total == 10000.0
    assert verdict.due_now == 3000.0


def test_schedule_that_underpays_is_a_discount_and_refused():
    verdict = evaluate_plan_schedule(
        10000.0,
        [{"amount": 3000, "due_date": "2026-09-01"}, {"amount": 2000, "due_date": "2026-09-04"}],
    )
    assert verdict.approved is False
    assert verdict.reason_code
    # The customer is told the shortfall, not just refused.
    assert verdict.reason


def test_first_payment_below_the_floor_is_refused():
    verdict = evaluate_plan_schedule(
        10000.0,
        [{"amount": 100, "due_date": "2026-09-01"}, {"amount": 9900, "due_date": "2026-09-04"}],
    )
    assert verdict.approved is False


def test_too_many_installments_is_refused():
    rows = [{"amount": 1000, "due_date": f"2026-09-0{index}"} for index in range(1, 10)]
    verdict = evaluate_plan_schedule(9000.0, rows)
    assert verdict.approved is False


# --------------------------------------------------------------------------- #
# Negotiation may only offer Confirm when the gate approved
# --------------------------------------------------------------------------- #

def test_negotiate_offers_confirmation_only_for_an_approved_schedule(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    turn = plan_chat.negotiate(plan, "I can pay Rs 3,000 today and Rs 7,000 on 4 September")
    assert turn["approved"] is True
    assert turn["awaiting_confirmation"] is True
    assert len(turn["installments"]) == 2
    assert turn["due_now"] == 3000.0


def test_negotiate_refuses_a_schedule_that_underpays(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    turn = plan_chat.negotiate(plan, "I can pay Rs 3,000 today and Rs 2,000 next week")
    assert turn["awaiting_confirmation"] is False
    assert turn["reason"]


def test_opening_message_already_knows_the_case(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    opening = plan_chat.opening_message(plan)
    assert "Aditya" in opening
    assert "10,000" in opening


# --------------------------------------------------------------------------- #
# Tokens authorise exactly one case, and nothing else
# --------------------------------------------------------------------------- #

def test_token_resolves_to_its_own_plan_only(plan_db: Path):
    plan, token = _open_plan(plan_db)
    assert flexible_plans.get_plan_by_token(token, path=plan_db)["id"] == plan["id"]
    assert flexible_plans.get_plan_by_token("not-a-real-token", path=plan_db) is None


def test_only_the_token_hash_is_stored(plan_db: Path):
    _plan, token = _open_plan(plan_db)
    stored = json.dumps(flexible_plans.list_plans(path=plan_db), default=str)
    assert token not in stored


def test_reinviting_a_paying_case_is_refused(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    flexible_plans.confirm_plan(plan["id"], [{"amount": 10000, "due_date": "2026-09-04"}], path=plan_db)
    flexible_plans._write(plan["id"], {"status": "active"}, plan_db)
    with pytest.raises(flexible_plans.PlanError):
        _open_plan(plan_db)


# --------------------------------------------------------------------------- #
# Confirmation freezes the schedule; nothing under it may be discounted
# --------------------------------------------------------------------------- #

def test_confirm_plan_freezes_the_schedule(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    confirmed = flexible_plans.confirm_plan(
        plan["id"],
        [{"amount": 3000, "due_date": "2026-09-01"}, {"amount": 7000, "due_date": "2026-09-04"}],
        path=plan_db,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["installment_count"] == 2
    assert confirmed["amount_paid"] == 0.0
    assert confirmed["amount_remaining"] == 10000.0
    assert "3,000" in confirmed["plan_summary"]


def test_confirm_plan_rejects_a_total_below_the_debt(plan_db: Path):
    plan, _token = _open_plan(plan_db)
    with pytest.raises(flexible_plans.PlanError):
        flexible_plans.confirm_plan(plan["id"], [{"amount": 4000, "due_date": "2026-09-04"}], path=plan_db)


def test_confirm_plan_accepts_the_gate_s_frozen_tuple(plan_db: Path):
    """``PlanVerdict.installments`` is a tuple; rejecting it emptied real plans."""
    plan, _token = _open_plan(plan_db)
    verdict = evaluate_plan_schedule(
        10000.0,
        [{"amount": 3000, "due_date": "2026-09-01"}, {"amount": 7000, "due_date": "2026-09-04"}],
    )
    assert isinstance(verdict.installments, tuple)
    confirmed = flexible_plans.confirm_plan(plan["id"], verdict.installments, path=plan_db)
    assert confirmed["installment_count"] == 2


# --------------------------------------------------------------------------- #
# Billing one installment: a new link, an email, an audit row
# --------------------------------------------------------------------------- #

class _Gmail:
    """Records what would have been emailed instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to_email: str, subject: str, body: str, **_kwargs):
        self.sent.append({"to": to_email, "subject": subject, "body": body})
        return {"id": f"msg_{len(self.sent)}"}


def _bill(
    plan: dict,
    plan_db: Path,
    audit: Path,
    gmail: _Gmail,
    monkeypatch,
    link_id: str = "plink_new_1",
    url: str = "https://rzp.io/i/new1",
):
    """Bill the next installment with the provider and mailer stubbed.

    Both are patched where ``plan_outreach`` imports them from — inside the
    function body — so no real Razorpay link is minted and no mail leaves.
    """
    monkeypatch.setattr(messenger, "send_email", gmail.send)
    monkeypatch.setattr(payments, "create_payment_link", lambda *a, **k: {"id": link_id, "short_url": url})
    return plan_outreach.send_installment_link(plan, case=CASE, plan_path=plan_db, audit_path=audit)


def test_billing_creates_a_new_link_and_emails_it(tmp_path: Path, plan_db: Path, monkeypatch):
    audit = tmp_path / "audit.csv"
    plan, _token = _open_plan(plan_db)
    confirmed = flexible_plans.confirm_plan(
        plan["id"],
        [{"amount": 3000, "due_date": "2026-09-01"}, {"amount": 7000, "due_date": "2026-09-04"}],
        path=plan_db,
    )
    gmail = _Gmail()
    result = _bill(confirmed, plan_db, audit, gmail, monkeypatch)

    assert result["link_url"] == "https://rzp.io/i/new1"
    assert result["sent"] is True
    # Billed for the FIRST installment only, never the whole debt.
    assert result["installment"] == 1
    assert result["amount"] == 3000.0
    assert len(gmail.sent) == 1
    assert gmail.sent[0]["to"] == "aditya@example.com"
    assert gmail.sent[0]["subject"].startswith("Flexible Payment Plan Confirmed")
    assert "3,000" in gmail.sent[0]["body"]
    # The link is bound to the installment it was minted for.
    stored = flexible_plans.get_plan(plan["id"], path=plan_db)
    assert stored["installments"][0]["link_id"] == "plink_new_1"
    assert stored["status"] in {"link_sent", "active"}


# --------------------------------------------------------------------------- #
# The webhook credits the plan AND the original case
# --------------------------------------------------------------------------- #

SECRET = "webhook-secret"


def _paid_delivery(link_id: str, amount_inr: float, plan_id: int, installment: int) -> tuple[bytes, str]:
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "amount": int(amount_inr * 100),
                    "amount_paid": int(amount_inr * 100),
                    "payment_id": f"pay_for_{link_id}",
                    "customer": {"name": "Aditya", "email": "aditya@example.com"},
                    "notes": {
                        "client_id": "CASE_1",
                        "client_name": "Aditya",
                        "client_email": "aditya@example.com",
                        "recovery_action": "resend_payment_link",
                        "flexible_plan_id": str(plan_id),
                        "flexible_plan_installment": str(installment),
                    },
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return body, hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _confirmed_and_billed(tmp_path: Path, plan_db: Path, monkeypatch) -> dict:
    audit = tmp_path / "audit.csv"
    plan, _token = _open_plan(plan_db)
    confirmed = flexible_plans.confirm_plan(
        plan["id"],
        [{"amount": 3000, "due_date": "2026-09-01"}, {"amount": 7000, "due_date": "2026-09-04"}],
        path=plan_db,
    )
    _bill(confirmed, plan_db, audit, _Gmail(), monkeypatch)
    return flexible_plans.get_plan(plan["id"], path=plan_db)


def test_webhook_credits_the_installment_to_the_original_case(tmp_path: Path, plan_db: Path, monkeypatch):
    audit = tmp_path / "audit.csv"
    plan = _confirmed_and_billed(tmp_path, plan_db, monkeypatch)
    body, signature = _paid_delivery("plink_new_1", 3000.0, plan["id"], 1)

    result = razorpay_webhooks.ingest_webhook(
        body, signature, SECRET, "evt_plan_1",
        webhook_path=tmp_path / "webhooks.sqlite3",
        audit_path=audit,
        recovery_path=tmp_path / "recoveries.sqlite3",
        plan_path=plan_db,
    )

    assert result["duplicate"] is False
    credit = result["plan_credit"]
    assert credit is not None and credit["duplicate"] is False
    assert credit["completed"] is False

    # The plan advanced by exactly one installment.
    updated = flexible_plans.get_plan(plan["id"], path=plan_db)
    assert updated["status"] == "active"
    assert updated["amount_paid"] == 3000.0
    assert updated["amount_remaining"] == 7000.0
    assert updated["next_installment"]["amount"] == 7000.0

    # The money landed on the ORIGINAL case, not a new one.
    record = razorpay_webhooks.get_recovery_record("CASE_1", path=tmp_path / "recoveries.sqlite3")
    assert record is not None
    assert record["amount_recovered"] == 3000.0

    # And the case history says so.
    actions = [row["action"] for row in read_events(audit)]
    assert "flexible_plan_installment_paid" in actions


def test_a_redelivered_payment_cannot_inflate_the_recovered_total(tmp_path: Path, plan_db: Path, monkeypatch):
    audit = tmp_path / "audit.csv"
    plan = _confirmed_and_billed(tmp_path, plan_db, monkeypatch)
    body, signature = _paid_delivery("plink_new_1", 3000.0, plan["id"], 1)
    stores = {
        "webhook_path": tmp_path / "webhooks.sqlite3",
        "audit_path": audit,
        "recovery_path": tmp_path / "recoveries.sqlite3",
        "plan_path": plan_db,
    }

    razorpay_webhooks.ingest_webhook(body, signature, SECRET, "evt_plan_1", **stores)
    second = razorpay_webhooks.ingest_webhook(body, signature, SECRET, "evt_plan_1", **stores)

    assert second["duplicate"] is True
    updated = flexible_plans.get_plan(plan["id"], path=plan_db)
    assert updated["amount_paid"] == 3000.0
    assert updated["installments_paid"] == 1


def test_paying_every_installment_completes_the_plan(tmp_path: Path, plan_db: Path, monkeypatch):
    audit = tmp_path / "audit.csv"
    plan = _confirmed_and_billed(tmp_path, plan_db, monkeypatch)
    stores = {
        "webhook_path": tmp_path / "webhooks.sqlite3",
        "audit_path": audit,
        "recovery_path": tmp_path / "recoveries.sqlite3",
        "plan_path": plan_db,
    }

    body, signature = _paid_delivery("plink_new_1", 3000.0, plan["id"], 1)
    razorpay_webhooks.ingest_webhook(body, signature, SECRET, "evt_plan_1", **stores)

    # Bill and pay the second installment.
    _bill(
        flexible_plans.get_plan(plan["id"], path=plan_db),
        plan_db,
        audit,
        _Gmail(),
        monkeypatch,
        link_id="plink_new_2",
        url="https://rzp.io/i/new2",
    )

    body2, signature2 = _paid_delivery("plink_new_2", 7000.0, plan["id"], 2)
    result = razorpay_webhooks.ingest_webhook(body2, signature2, SECRET, "evt_plan_2", **stores)

    assert result["plan_credit"]["completed"] is True
    updated = flexible_plans.get_plan(plan["id"], path=plan_db)
    assert updated["status"] == "completed"
    assert updated["amount_remaining"] == 0.0


def test_an_ordinary_recovery_payment_is_untouched_by_the_plan_path(tmp_path: Path, plan_db: Path):
    """A link with no plan notes must behave exactly as it did before."""
    audit = tmp_path / "audit.csv"
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_plain",
                    "amount": 500000,
                    "amount_paid": 500000,
                    "customer": {"name": "Riya", "email": "riya@example.com"},
                    "notes": {"client_id": "CASE_2", "recovery_action": "retry_payment"},
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    result = razorpay_webhooks.ingest_webhook(
        body, signature, SECRET, "evt_plain",
        webhook_path=tmp_path / "webhooks.sqlite3",
        audit_path=audit,
        recovery_path=tmp_path / "recoveries.sqlite3",
        plan_path=plan_db,
    )

    assert result["plan_credit"] is None
    record = razorpay_webhooks.get_recovery_record("CASE_2", path=tmp_path / "recoveries.sqlite3")
    assert record["amount_recovered"] == 5000.0


def test_normalize_carries_the_plan_thread_from_the_notes():
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_x",
                    "amount": 300000,
                    "amount_paid": 300000,
                    "notes": {
                        "client_id": "CASE_1",
                        "recovery_action": "resend_payment_link",
                        "flexible_plan_id": "12",
                        "flexible_plan_installment": "2",
                    },
                }
            }
        },
    }
    normalized = razorpay_webhooks.normalize_webhook(payload, event_id="evt_n")
    assert normalized["flexible_plan_id"] == "12"
    assert normalized["flexible_plan_installment"] == "2"


# --- The dashboard's view of a plan --------------------------------------------
#
# ``list_clients`` is the one place the operator's case row is assembled. A plan
# must appear there as extra facts about the SAME case, never as a case of its
# own, and a case without a plan must read exactly as it did before.


def _service(tmp_path: Path, plan_db: Path):
    from modules.service_layer import RecoveryService

    return RecoveryService(
        audit_path=tmp_path / "audit.csv",
        attempts_path=tmp_path / "attempts.sqlite3",
        waitlist_path=tmp_path / "waitlist.sqlite3",
        recovery_path=tmp_path / "recoveries.sqlite3",
        plan_path=plan_db,
    )


def test_dashboard_case_row_carries_the_plan(tmp_path: Path, plan_db: Path, monkeypatch):
    audit = tmp_path / "audit.csv"
    plan = _confirmed_and_billed(tmp_path, plan_db, monkeypatch)
    body, signature = _paid_delivery("plink_new_1", 3000.0, plan["id"], 1)
    razorpay_webhooks.ingest_webhook(
        body, signature, SECRET, "evt_plan_1",
        webhook_path=tmp_path / "webhooks.sqlite3", audit_path=audit,
        recovery_path=tmp_path / "recoveries.sqlite3", plan_path=plan_db,
    )

    rows = {row["client_id"]: row for row in _service(tmp_path, plan_db).list_clients()}
    row = rows["CASE_1"]

    # Spec section 10's copy, straight from display_status - no new strings invented.
    assert row["plan_outcome"] == "Payment Plan Active"
    assert row["plan_status"] == "active"
    assert "3,000" in row["plan_summary"]
    # Part-paid: the plan's cumulative rows, not the latest single payment.
    assert row["amount_recovered"] == 3000.0
    assert row["amount_remaining"] == 7000.0
    # The existing key keeps its meaning: the confirmed payment state of the case.
    assert row["payment_status"] == "partially_paid"
    assert row["plan_installments_paid"] == 1
    assert row["plan_installment_count"] == 2
    assert row["plan_next_amount"] == 7000.0


def test_dashboard_case_without_a_plan_is_unchanged(tmp_path: Path, plan_db: Path):
    from modules.audit_log import log_event

    log_event(
        {"client_id": "CASE_9", "client_name": "Riya", "client_email": "riya@example.com", "amount": 4000.0},
        "resend_payment_link", None, "link_created", tmp_path / "audit.csv",
    )

    rows = {row["client_id"]: row for row in _service(tmp_path, plan_db).list_clients()}
    row = rows["CASE_9"]

    assert row["plan_status"] == ""
    assert row["plan_outcome"] == ""
    assert row["plan_summary"] == ""
    assert row["plan_installments"] == []
    assert row["amount_remaining"] is None
