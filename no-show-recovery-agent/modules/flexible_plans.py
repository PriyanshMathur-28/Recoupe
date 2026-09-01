"""Flexible payment plans: the ``payment_plan`` store and its access tokens.

This module is the mutable state behind one feature: a customer who told the
voice agent they cannot pay in full is emailed a private link, negotiates a
split of the SAME debt with a chatbot, and pays it in installments.

Design rules, chosen to fit the stores that already exist:

* **One row per CASE, not per conversation.** A plan is an alternative
  settlement of one recovery case, so ``case_id`` is unique. Re-inviting a
  customer reuses the row and re-issues its token; it never forks the case,
  which is what keeps voice attribution intact (spec: the recovered amount
  stays credited to the original case).

* **No new event_type, no new recovery_action.** ``revenue_event.EVENT_TYPES``
  and ``razorpay_webhooks.normalize_webhook``'s allow-list both reject unknown
  values, so a plan carries the original case's event type and its links ride
  the existing ``resend_payment_link`` action. A plan is recognised on the way
  back by ``notes.flexible_plan_id``, not by a new action name.

* **No stored counters.** ``amount_paid`` and the remaining balance are derived
  from the installment rows every time they are read, exactly like the voice
  metric cards, so a total can never drift from the payments it describes.

* **The token is a bearer secret, so only its hash is stored.** A leaked
  database cannot be replayed against the customer endpoint, and a token is
  scoped to exactly one ``case_id`` and expires.

* **Append-only audit stays append-only.** ``audit_log.log_event`` has no update
  path, so the plan's *current* state lives here and the audit trail records
  only its transitions.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLAN_DB_PATH = ROOT / "data" / "flexible_plans.sqlite3"

# Appending here is all that is needed: _connect() widens an existing
# payment_plan table to match this tuple on the next open, the same idiom
# voice_calls.CALL_FIELDS uses.
PLAN_FIELDS: tuple[str, ...] = (
    "case_id",
    "case_key",
    "client_name",
    "client_email",
    "event_type",
    "original_amount",
    "currency",
    "origin",
    "origin_call_id",
    "voice_hint",
    "status",
    "token_hash",
    "token_expires_at",
    "created_at",
    "updated_at",
    "confirmed_at",
    "completed_at",
    "installments_json",
    "plan_summary",
)

# The plan lifecycle, in order. Every transition is checked against this tuple,
# so a plan can never be emailed a link it has not confirmed, or completed
# without a payment.
PLAN_STATUSES: tuple[str, ...] = (
    "invited",      # the chatbot link has been emailed; nothing negotiated yet
    "negotiating",  # the customer has proposed at least one arrangement
    "confirmed",    # the customer pressed Confirm Plan on a policy-valid plan
    "link_sent",    # a Razorpay link for the next installment has been emailed
    "active",       # at least one installment is paid, but not all of them
    "completed",    # every installment is paid
    "expired",      # the access token lapsed before the plan was confirmed
)

INSTALLMENT_STATUSES: tuple[str, ...] = ("pending", "link_sent", "paid")

# Audit actions for the plan lifecycle. These are deliberately OUTSIDE
# service_layer.CASE_ACTIONS — the same technique voice_calls.VOICE_LINK_ACTION
# uses — so writing them can never rewrite the case's current condition, and
# outside voice_calls.EMAIL_SENT_OUTCOMES so they cannot steal voice attribution.
PLAN_REQUESTED_ACTION = "flexible_plan_requested"
PLAN_INVITED_ACTION = "flexible_plan_invited"
PLAN_CONFIRMED_ACTION = "flexible_plan_confirmed"
PLAN_LINK_ACTION = "flexible_plan_link_sent"
PLAN_PAYMENT_ACTION = "flexible_plan_installment_paid"
PLAN_COMPLETED_ACTION = "flexible_plan_completed"
PLAN_ACTIONS = frozenset({
    PLAN_REQUESTED_ACTION,
    PLAN_INVITED_ACTION,
    PLAN_CONFIRMED_ACTION,
    PLAN_LINK_ACTION,
    PLAN_PAYMENT_ACTION,
    PLAN_COMPLETED_ACTION,
})


class PlanError(ValueError):
    """A plan was asked to do something its own contract forbids."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _money(value: Any) -> float:
    """Round to whole paise; anything unreadable is zero, never an exception."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def token_ttl_hours() -> int:
    """Lifetime of a chatbot access token, overridable per deployment."""
    try:
        hours = int(str(os.getenv("FLEX_PLAN_TOKEN_TTL_HOURS") or "168").strip())
    except ValueError:
        return 168
    return hours if hours > 0 else 168


def hash_token(token: str) -> str:
    """Hash a bearer token for storage and constant-shape lookup."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """Return ``(token, token_hash, expires_at)`` for one fresh access token.

    ``secrets.token_urlsafe(32)`` is 256 bits of entropy, so the URL cannot be
    guessed, and the expiry is absolute rather than sliding so an old email
    cannot be revived by using it.
    """
    token = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(hours=token_ttl_hours())).isoformat()
    return token, hash_token(token), expires_at


def _connect(path: Path = PLAN_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    columns = ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in PLAN_FIELDS)
    connection.execute(f"CREATE TABLE IF NOT EXISTS payment_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, {columns})")
    existing = {row[1] for row in connection.execute("PRAGMA table_info(payment_plan)")}
    for field in PLAN_FIELDS:
        if field not in existing:
            connection.execute(f"ALTER TABLE payment_plan ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS payment_plan_case ON payment_plan (case_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS payment_plan_token ON payment_plan (token_hash)")
    # One payment can only ever be counted once, whatever a provider retries.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS plan_payment ("
        "payment_id TEXT PRIMARY KEY, plan_id INTEGER NOT NULL, installment INTEGER NOT NULL, "
        "amount TEXT NOT NULL, paid_at TEXT NOT NULL, link_id TEXT NOT NULL DEFAULT '')"
    )
    connection.commit()
    return connection


def normalize_installments(installments: Any) -> list[dict[str, Any]]:
    """Coerce a proposed schedule to the stored installment contract.

    Returns rows shaped ``{"index", "amount", "due_date", "status", "paid_at",
    "payment_id", "link_id", "link_url"}``. Ordering is by index so "the next
    installment" is always well defined.

    Tuples are accepted as well as lists, because the approved schedule arrives
    as :attr:`modules.policy_engine.PlanVerdict.installments`, which is a frozen
    tuple. Rejecting it here would have silently emptied a confirmed plan.
    """
    rows: list[dict[str, Any]] = []
    for position, raw in enumerate(installments if isinstance(installments, (list, tuple)) else [], start=1):
        if not isinstance(raw, dict):
            continue
        amount = _money(raw.get("amount"))
        if amount <= 0:
            continue
        status = str(raw.get("status") or "pending")
        rows.append({
            "index": position,
            "amount": amount,
            "due_date": str(raw.get("due_date") or "").strip(),
            "status": status if status in INSTALLMENT_STATUSES else "pending",
            "paid_at": str(raw.get("paid_at") or "").strip(),
            "payment_id": str(raw.get("payment_id") or "").strip(),
            "link_id": str(raw.get("link_id") or "").strip(),
            "link_url": str(raw.get("link_url") or "").strip(),
        })
    return rows


def _row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
    """Expand one stored row into the derived shape every caller reads."""
    plan: dict[str, Any] = {"id": int(row["id"])}
    for field in PLAN_FIELDS:
        plan[field] = row[field]
    plan["original_amount"] = _money(row["original_amount"])
    try:
        installments = normalize_installments(json.loads(row["installments_json"] or "[]"))
    except (TypeError, ValueError):
        installments = []
    plan["installments"] = installments
    plan.pop("installments_json", None)
    # Derived, never stored: totals are a live read over the installment rows.
    paid_rows = [item for item in installments if item["status"] == "paid"]
    plan["amount_paid"] = round(sum(item["amount"] for item in paid_rows), 2)
    plan["amount_remaining"] = round(max(plan["original_amount"] - plan["amount_paid"], 0.0), 2)
    plan["installments_total"] = round(sum(item["amount"] for item in installments), 2)
    plan["installments_paid"] = len(paid_rows)
    plan["installment_count"] = len(installments)
    plan["next_installment"] = next((item for item in installments if item["status"] != "paid"), None)
    plan["token_expired"] = is_expired(plan)
    plan["display_status"] = display_status(plan)
    return plan


def is_expired(plan: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the plan's access token has lapsed."""
    expires_at = str(plan.get("token_expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (now or _now()) >= deadline


def display_status(plan: dict[str, Any]) -> str:
    """The operator-facing label the dashboard shows for this plan."""
    return {
        "invited": "Flexible Plan Requested",
        "negotiating": "Flexible Plan In Discussion",
        "confirmed": "Flexible Payment Plan Confirmed",
        "link_sent": "Flexible Payment Plan Confirmed",
        "active": "Payment Plan Active",
        "completed": "Payment Plan Completed",
        "expired": "Flexible Plan Link Expired",
    }.get(str(plan.get("status") or ""), "Flexible Payment Plan")


def plan_summary_line(installments: list[dict[str, Any]]) -> str:
    """Render a schedule the way the dashboard shows it: ``₹3,000 today + ₹7,000 Sept 4``."""
    parts: list[str] = []
    for item in installments:
        amount = f"Rs {item['amount']:,.0f}"
        when = _friendly_date(item.get("due_date"))
        parts.append(f"{amount} {when}" if when else amount)
    return " + ".join(parts)


def _friendly_date(value: Any) -> str:
    """Turn an ISO date into ``today`` / ``Sept 4``; blank stays blank."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text
    today = _now().date()
    if parsed == today:
        return "today"
    if parsed == today + timedelta(days=1):
        return "tomorrow"
    return parsed.strftime("%b %-d") if os.name != "nt" else parsed.strftime("%b %d").replace(" 0", " ")


def create_or_refresh_plan(
    case: dict[str, Any],
    *,
    origin: str = "voice_recovery",
    origin_call_id: Any = None,
    voice_hint: str = "",
    path: Path = PLAN_DB_PATH,
) -> tuple[dict[str, Any], str]:
    """Open (or re-open) the plan for one case and issue a fresh access token.

    ``case`` is a recovery case as ``service_layer.RecoveryService.list_clients``
    returns it — the plan reads the billable facts from there so it can never
    bill an amount the dashboard is not showing.

    Returns ``(plan, token)``. The plaintext token is returned exactly once,
    here, because only its hash is stored.

    Re-inviting a case that is already paying is refused: an active plan's
    schedule is a commitment, and handing out a new token would let a second
    negotiation quietly replace it.
    """
    case_id = str(case.get("client_id") or case.get("case_id") or "").strip()
    client_email = str(case.get("client_email") or (case.get("case") or {}).get("client_email") or "").strip()
    if not case_id:
        raise PlanError("A flexible plan needs the recovery case id it settles.")
    if "@" not in client_email:
        raise PlanError("A flexible plan needs the customer's email address to reach them.")
    event = dict(case.get("case") or {})
    amount = _money(
        case.get("amount")
        or event.get("amount")
        or event.get("fee_amount")
        or event.get("appointment_value")
        or event.get("subscription_amount")
    )
    if amount <= 0:
        raise PlanError("A flexible plan needs a positive outstanding amount to split.")

    token, token_hash, expires_at = mint_token()
    existing = get_plan_by_case(case_id, path=path)
    if existing and existing["status"] in {"active", "completed"}:
        raise PlanError(f"Case {case_id} already has a {existing['status']} payment plan.")

    with _connect(path) as connection:
        if existing:
            connection.execute(
                "UPDATE payment_plan SET status = 'invited', token_hash = ?, token_expires_at = ?, "
                "updated_at = ?, client_email = ?, original_amount = ?, voice_hint = ?, "
                "origin = ?, origin_call_id = ?, installments_json = '[]', plan_summary = '', confirmed_at = '' "
                "WHERE id = ?",
                (
                    token_hash, expires_at, _stamp(), client_email, f"{amount}",
                    str(voice_hint or existing.get("voice_hint") or ""),
                    str(origin or "voice_recovery"), str(origin_call_id or existing.get("origin_call_id") or ""),
                    existing["id"],
                ),
            )
            plan_id = existing["id"]
        else:
            values = {
                "case_id": case_id,
                "case_key": str(case.get("case_key") or ""),
                "client_name": str(case.get("client_name") or event.get("client_name") or "there"),
                "client_email": client_email,
                "event_type": str(event.get("event_type") or ""),
                "original_amount": f"{amount}",
                "currency": str(event.get("currency") or "INR"),
                "origin": str(origin or "voice_recovery"),
                "origin_call_id": str(origin_call_id or ""),
                "voice_hint": str(voice_hint or ""),
                "status": "invited",
                "token_hash": token_hash,
                "token_expires_at": expires_at,
                "created_at": _stamp(),
                "updated_at": _stamp(),
                "confirmed_at": "",
                "completed_at": "",
                "installments_json": "[]",
                "plan_summary": "",
            }
            placeholders = ", ".join("?" for _ in PLAN_FIELDS)
            cursor = connection.execute(
                f"INSERT INTO payment_plan ({', '.join(PLAN_FIELDS)}) VALUES ({placeholders})",
                tuple(values[field] for field in PLAN_FIELDS),
            )
            plan_id = int(cursor.lastrowid)
        row = connection.execute("SELECT * FROM payment_plan WHERE id = ?", (plan_id,)).fetchone()
    return _row_to_plan(row), token


def get_plan(plan_id: int, path: Path = PLAN_DB_PATH) -> dict[str, Any] | None:
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM payment_plan WHERE id = ?", (int(plan_id),)).fetchone()
    return _row_to_plan(row) if row else None


def get_plan_by_case(case_id: str, path: Path = PLAN_DB_PATH) -> dict[str, Any] | None:
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM payment_plan WHERE case_id = ?", (str(case_id),)).fetchone()
    return _row_to_plan(row) if row else None


def get_plan_by_token(token: str, path: Path = PLAN_DB_PATH) -> dict[str, Any] | None:
    """Resolve a bearer token to its ONE plan, or None.

    Lookup is by hash, so the stored row never contains the secret. A token that
    resolves but has expired is returned with ``token_expired`` true rather than
    hidden, so the customer can be told why the page will not open instead of
    seeing a bare 404.
    """
    candidate = str(token or "").strip()
    if not candidate:
        return None
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM payment_plan WHERE token_hash = ?", (hash_token(candidate),)).fetchone()
    return _row_to_plan(row) if row else None


def list_plans(path: Path = PLAN_DB_PATH) -> dict[str, dict[str, Any]]:
    """Every plan keyed by ``case_id``, for the dashboard's case assembly."""
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM payment_plan ORDER BY id").fetchall()
    return {str(row["case_id"]): _row_to_plan(row) for row in rows}


def _write(plan_id: int, updates: dict[str, Any], path: Path) -> dict[str, Any]:
    fields = {**updates, "updated_at": _stamp()}
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with _connect(path) as connection:
        cursor = connection.execute(
            f"UPDATE payment_plan SET {assignments} WHERE id = ?",
            (*(str(value) for value in fields.values()), int(plan_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"No payment plan with id {plan_id}")
        row = connection.execute("SELECT * FROM payment_plan WHERE id = ?", (int(plan_id),)).fetchone()
    return _row_to_plan(row)


def mark_negotiating(plan_id: int, path: Path = PLAN_DB_PATH) -> dict[str, Any]:
    """Record that the customer has started proposing arrangements.

    Only an ``invited`` plan advances; a confirmed or paying plan is left alone
    so that continuing to chat cannot silently reopen a settled schedule.
    """
    plan = get_plan(plan_id, path=path)
    if plan is None:
        raise LookupError(f"No payment plan with id {plan_id}")
    if plan["status"] != "invited":
        return plan
    return _write(plan_id, {"status": "negotiating"}, path)


def confirm_plan(plan_id: int, installments: list[dict[str, Any]], path: Path = PLAN_DB_PATH) -> dict[str, Any]:
    """Freeze the customer-confirmed schedule onto the plan.

    Refused unless the plan is still being negotiated and the schedule settles
    the whole outstanding amount: a plan that adds up to less than the debt is a
    discount, and no discount can be granted from the customer's side of the
    conversation.
    """
    plan = get_plan(plan_id, path=path)
    if plan is None:
        raise LookupError(f"No payment plan with id {plan_id}")
    if plan["status"] not in {"invited", "negotiating", "confirmed"}:
        raise PlanError(f"A {plan['status']} plan cannot be confirmed again.")
    if plan["token_expired"]:
        raise PlanError("This flexible payment link has expired.")
    rows = normalize_installments(installments)
    if not rows:
        raise PlanError("A confirmed plan needs at least one installment.")
    total = round(sum(item["amount"] for item in rows), 2)
    if abs(total - plan["original_amount"]) > 0.5:
        raise PlanError(
            f"The plan totals Rs {total:,.0f} but the outstanding amount is Rs {plan['original_amount']:,.0f}."
        )
    return _write(
        plan_id,
        {
            "status": "confirmed",
            "confirmed_at": _stamp(),
            "installments_json": json.dumps(rows),
            "plan_summary": plan_summary_line(rows),
        },
        path,
    )


def attach_installment_link(
    plan_id: int,
    index: int,
    link_id: str,
    link_url: str,
    path: Path = PLAN_DB_PATH,
) -> dict[str, Any]:
    """Bind a Razorpay link to one installment and mark the plan ``link_sent``."""
    plan = get_plan(plan_id, path=path)
    if plan is None:
        raise LookupError(f"No payment plan with id {plan_id}")
    if plan["status"] not in {"confirmed", "link_sent", "active"}:
        raise PlanError(f"A {plan['status']} plan has no confirmed installment to bill.")
    rows = plan["installments"]
    target = next((item for item in rows if item["index"] == int(index)), None)
    if target is None:
        raise PlanError(f"Installment {index} is not part of this plan.")
    if target["status"] == "paid":
        raise PlanError(f"Installment {index} is already paid.")
    target.update({"status": "link_sent", "link_id": str(link_id or ""), "link_url": str(link_url or "")})
    status = "active" if any(item["status"] == "paid" for item in rows) else "link_sent"
    return _write(plan_id, {"status": status, "installments_json": json.dumps(rows)}, path)


def find_plan_for_payment(
    plan_id: Any = None,
    link_id: str = "",
    path: Path = PLAN_DB_PATH,
) -> dict[str, Any] | None:
    """Resolve an incoming payment to its plan by note id, then by link id.

    The note is authoritative because we put it there; the link id is the
    fallback for a webhook that lost its notes.
    """
    if str(plan_id or "").strip().isdigit():
        plan = get_plan(int(str(plan_id).strip()), path=path)
        if plan is not None:
            return plan
    reference = str(link_id or "").strip()
    if not reference:
        return None
    for plan in list_plans(path=path).values():
        if any(item["link_id"] == reference for item in plan["installments"]):
            return plan
    return None


def record_installment_payment(
    plan_id: int,
    *,
    payment_id: str,
    amount: Any,
    link_id: str = "",
    paid_at: str = "",
    path: Path = PLAN_DB_PATH,
) -> dict[str, Any]:
    """Credit one installment payment, exactly once.

    Idempotency is enforced by ``plan_payment.payment_id``, so a provider that
    redelivers the same payment cannot inflate the recovered total. Returns
    ``{"plan", "installment", "duplicate", "completed"}``.

    The installment credited is matched by link id first and otherwise by the
    next unpaid row, so a customer who pays from an old email still lands on the
    installment that link was minted for.
    """
    plan = get_plan(plan_id, path=path)
    if plan is None:
        raise LookupError(f"No payment plan with id {plan_id}")
    reference = str(payment_id or "").strip()
    if not reference:
        raise PlanError("An installment payment needs the provider's payment id.")
    rows = plan["installments"]
    if not rows:
        raise PlanError("This plan has no confirmed installments to credit.")

    link_reference = str(link_id or "").strip()
    target = next((item for item in rows if link_reference and item["link_id"] == link_reference and item["status"] != "paid"), None)
    target = target or next((item for item in rows if item["status"] != "paid"), None)
    if target is None:
        return {"plan": plan, "installment": None, "duplicate": True, "completed": True}

    with _connect(path) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO plan_payment (payment_id, plan_id, installment, amount, paid_at, link_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reference, int(plan_id), int(target["index"]), f"{_money(amount)}", paid_at or _stamp(), link_reference),
        )
        duplicate = cursor.rowcount == 0
    if duplicate:
        return {"plan": plan, "installment": None, "duplicate": True, "completed": plan["status"] == "completed"}

    target.update({"status": "paid", "paid_at": paid_at or _stamp(), "payment_id": reference})
    completed = all(item["status"] == "paid" for item in rows)
    updates = {
        "status": "completed" if completed else "active",
        "installments_json": json.dumps(rows),
    }
    if completed:
        updates["completed_at"] = paid_at or _stamp()
    updated = _write(plan_id, updates, path)
    return {"plan": updated, "installment": dict(target), "duplicate": False, "completed": completed}


def expire_stale_plans(path: Path = PLAN_DB_PATH) -> int:
    """Mark unconfirmed plans whose token lapsed as ``expired``. Returns the count."""
    changed = 0
    for plan in list_plans(path=path).values():
        if plan["status"] in {"invited", "negotiating"} and plan["token_expired"]:
            _write(plan["id"], {"status": "expired"}, path)
            changed += 1
    return changed


def link_notes(plan: dict[str, Any], installment: dict[str, Any]) -> dict[str, str]:
    """Razorpay ``notes`` for an installment link.

    ``recovery_action`` stays inside the existing three-value allow-list that
    ``razorpay_webhooks.normalize_webhook`` raises on, so this link is a normal
    recovery link as far as every existing code path is concerned. The plan is
    recognised on the way back by ``flexible_plan_id``.
    """
    return {
        "client_id": str(plan.get("case_id") or ""),
        "client_name": str(plan.get("client_name") or ""),
        "client_email": str(plan.get("client_email") or ""),
        "recovery_action": "resend_payment_link",
        "flexible_plan_id": str(plan.get("id") or ""),
        "flexible_plan_installment": str(installment.get("index") or ""),
    }


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "plans.sqlite3"
        checks: list[tuple[str, bool]] = []

        def check(label: str, condition: bool) -> None:
            checks.append((label, bool(condition)))

        case = {
            "client_id": "C-1",
            "client_name": "Aditya",
            "client_email": "aditya@example.com",
            "case_key": "k1",
            "case": {"client_email": "aditya@example.com", "event_type": "no_show", "fee_amount": 10000},
        }
        plan, token = create_or_refresh_plan(case, origin_call_id=7, voice_hint="can pay 3000 today", path=db)
        check("a new plan starts invited", plan["status"] == "invited")
        check("the outstanding amount is read from the case", plan["original_amount"] == 10000.0)
        check("the plaintext token is not stored", plan["token_hash"] != token and len(token) > 20)
        check("the token resolves to its own plan", (get_plan_by_token(token, path=db) or {}).get("id") == plan["id"])
        check("an unknown token resolves to nothing", get_plan_by_token("nope", path=db) is None)

        try:
            confirm_plan(plan["id"], [{"amount": 3000, "due_date": ""}], path=db)
            check("a short plan is refused", False)
        except PlanError:
            check("a short plan is refused", True)

        schedule = [{"amount": 3000, "due_date": _now().date().isoformat()}, {"amount": 7000, "due_date": "2026-09-04"}]
        confirmed = confirm_plan(plan["id"], schedule, path=db)
        check("confirming freezes the schedule", confirmed["status"] == "confirmed" and confirmed["installment_count"] == 2)
        check("the summary reads like the dashboard", confirmed["plan_summary"].startswith("Rs 3,000 today + Rs 7,000"))
        check("nothing is recovered yet", confirmed["amount_paid"] == 0.0 and confirmed["amount_remaining"] == 10000.0)

        billed = attach_installment_link(plan["id"], 1, "plink_1", "https://rzp.io/i/one", path=db)
        check("billing the first installment does not touch the second", billed["installments"][1]["status"] == "pending")
        check("the plan is waiting on a payment", billed["display_status"] == "Flexible Payment Plan Confirmed")

        paid = record_installment_payment(plan["id"], payment_id="pay_1", amount=3000, link_id="plink_1", path=db)
        check("the first payment activates the plan", paid["plan"]["status"] == "active")
        check("recovered and remaining are derived", paid["plan"]["amount_paid"] == 3000.0 and paid["plan"]["amount_remaining"] == 7000.0)
        check("the dashboard label is Payment Plan Active", paid["plan"]["display_status"] == "Payment Plan Active")
        check("the plan is not complete on one payment", not paid["completed"])

        again = record_installment_payment(plan["id"], payment_id="pay_1", amount=3000, link_id="plink_1", path=db)
        check("a redelivered payment is a duplicate, not a second credit", again["duplicate"])
        check("a duplicate cannot inflate the total", (get_plan(plan["id"], path=db) or {})["amount_paid"] == 3000.0)

        final = record_installment_payment(plan["id"], payment_id="pay_2", amount=7000, path=db)
        check("the last payment completes the plan", final["completed"] and final["plan"]["status"] == "completed")
        check("a completed plan has nothing remaining", final["plan"]["amount_remaining"] == 0.0)

        check("notes stay inside the webhook allow-list", link_notes(final["plan"], {"index": 2})["recovery_action"] == "resend_payment_link")
        check("notes carry the plan back", link_notes(final["plan"], {"index": 2})["flexible_plan_id"] == str(plan["id"]))
        check("a payment finds its plan by link id", (find_plan_for_payment(link_id="plink_1", path=db) or {}).get("id") == plan["id"])

        try:
            create_or_refresh_plan(case, path=db)
            check("a paying case cannot be re-invited", False)
        except PlanError:
            check("a paying case cannot be re-invited", True)

        for label, ok in checks:
            print(f"{'PASS' if ok else 'FAIL'}: {label}")
        if not all(ok for _, ok in checks):
            raise SystemExit(1)
