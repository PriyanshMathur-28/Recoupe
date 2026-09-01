"""Razorpay webhook normalization, auditing, and idempotent event storage.

The webhook boundary accepts only verified Razorpay payloads, converts payment
success/failure notifications into the common event shape used by the agent, and
closes the audit loop when a payment link outcome arrives.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_log import AUDIT_PATH, log_event
from .revenue_event import RAZORPAY_FAILURE_EVENTS, from_razorpay_webhook

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_DB_PATH = ROOT / "data" / "webhook_events.sqlite3"
RECOVERY_DB_PATH = ROOT / "data" / "recovered_cases.sqlite3"
SUPPORTED_EVENTS = {
    "payment_link.paid", "payment_link.partially_paid", "payment.failed",
    "payment.authorized", "payment.captured", "subscription.charged.failed",
    "subscription.pending", "subscription.halted", "invoice.partially_paid",
    "invoice.expired", "payment_link.expired",
}


def verify_signature(body: bytes | str, signature: str, secret: str) -> bool:
    """Verify Razorpay's HMAC-SHA256 webhook signature in constant time."""
    if not signature or not secret:
        return False
    raw_body = body.encode("utf-8") if isinstance(body, str) else body
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def _connect(path: Path = WEBHOOK_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, event_name TEXT NOT NULL, received_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def record_once(event_id: str, event_name: str, payload: dict[str, Any], path: Path = WEBHOOK_DB_PATH) -> bool:
    """Record a webhook id once; return False for a duplicate delivery."""
    if not str(event_id or "").strip():
        raise ValueError("Razorpay webhook event_id is required")
    with _connect(path) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id, event_name, received_at, payload_json) VALUES (?, ?, ?, ?)",
            (str(event_id), event_name, datetime.now(timezone.utc).isoformat(), json.dumps(payload, sort_keys=True)),
        )
    return cursor.rowcount == 1


# Every column a caller may read back. ``recovered_via`` and
# ``recovery_triggered_at`` carry the attribution decision, which is made exactly
# once (at webhook time) and never recomputed by a later query.
RECOVERY_FIELDS = (
    "client_id",
    "payment_link_id",
    "amount_recovered",
    "recovered_at",
    "event_id",
    "event_name",
    "recovered_via",
    "recovery_triggered_at",
)


def _connect_recovery(path: Path = RECOVERY_DB_PATH) -> sqlite3.Connection:
    """Open the recovered-cases store, creating or widening the schema on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS recovered_cases ("
        "  client_id TEXT NOT NULL,"
        "  payment_link_id TEXT,"
        "  amount_recovered REAL NOT NULL,"
        "  recovered_at TEXT NOT NULL,"
        "  event_id TEXT NOT NULL,"
        "  event_name TEXT NOT NULL,"
        "  recovered_via TEXT NOT NULL DEFAULT '',"
        "  recovery_triggered_at TEXT NOT NULL DEFAULT '',"
        "  PRIMARY KEY (client_id, event_id)"
        ")"
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(recovered_cases)")}
    for column in ("recovered_via", "recovery_triggered_at"):
        if column not in existing:
            connection.execute(f"ALTER TABLE recovered_cases ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    connection.commit()
    return connection


def write_recovery_record(
    client_id: str,
    amount_recovered: float,
    payment_link_id: str | None,
    event_id: str,
    event_name: str,
    path: Path = RECOVERY_DB_PATH,
    recovered_via: str | None = None,
    recovery_triggered_at: str | None = None,
) -> bool:
    """Persist a confirmed recovery; return False on duplicate event_id.

    The amount, the payment instant, the attributed channel and the instant that
    channel acted are written in ONE statement. There is deliberately no
    follow-up UPDATE to fill in attribution, because a row that exists with
    ``recovered_at`` set but ``recovered_via`` still blank would make "₹
    recovered via voice" silently undercount for as long as the gap lasted.
    """
    recovered_at = datetime.now(timezone.utc).isoformat()
    with _connect_recovery(path) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO recovered_cases "
            "(client_id, payment_link_id, amount_recovered, recovered_at, event_id, event_name, "
            "recovered_via, recovery_triggered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(client_id),
                payment_link_id,
                float(amount_recovered),
                recovered_at,
                str(event_id),
                str(event_name),
                str(recovered_via or ""),
                str(recovery_triggered_at or ""),
            ),
        )
    return cursor.rowcount == 1


def get_recovery_record(client_id: str, path: Path = RECOVERY_DB_PATH) -> dict[str, Any] | None:
    """Return the most recent confirmed recovery for a client, or None."""
    with _connect_recovery(path) as connection:
        row = connection.execute(
            "SELECT " + ", ".join(RECOVERY_FIELDS) + " "
            "FROM recovered_cases WHERE client_id = ? ORDER BY recovered_at DESC LIMIT 1",
            (str(client_id),),
        ).fetchone()
    return dict(row) if row else None


def list_recovery_records(path: Path = RECOVERY_DB_PATH) -> dict[str, dict[str, Any]]:
    """Return all confirmed recovery records keyed by client_id (most recent per client)."""
    with _connect_recovery(path) as connection:
        rows = connection.execute(
            "SELECT " + ", ".join(RECOVERY_FIELDS) + " "
            "FROM recovered_cases ORDER BY recovered_at DESC"
        ).fetchall()
    # Keep only the most recent record per client.
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = str(row["client_id"])
        if cid not in result:
            result[cid] = dict(row)
    return result


def normalize_webhook(payload: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
    """Map a verified Razorpay webhook payload to a recovery event.

    ``event_id`` is Razorpay's delivery identity and is deliberately kept
    separate from the payment-link and customer identifiers in the payload.

    ``flexible_plan_id``/``flexible_plan_installment`` are carried through from
    the link's notes (see :func:`modules.flexible_plans.link_notes`). They are
    the only thread tying an installment payment back to the plan that minted
    its link, and therefore back to the ORIGINAL recovery case — the plan link
    is otherwise indistinguishable from an ordinary ``resend_payment_link``.
    """
    event_name = str(payload.get("event") or "")
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        raise ValueError("Razorpay webhook event_id is required")
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Razorpay webhook event: {event_name or 'missing event'}")
    container = payload.get("payload") or {}
    entity_key = "payment" if event_name in {"payment.authorized", "payment.captured"} else "payment_link"
    entity = (container.get(entity_key) or {}).get("entity") or {}
    if not isinstance(entity, dict) or not entity:
        raise ValueError(f"Razorpay webhook {entity_key} entity is missing")
    customer = entity.get("customer") or {}
    notes = entity.get("notes") if isinstance(entity.get("notes"), dict) else {}
    action = str(notes.get("recovery_action") or "retry_payment")
    if action not in {"charge_fee", "retry_payment", "resend_payment_link"}:
        raise ValueError("Webhook recovery_action is outside the bounded action allow-list")
    amount = entity.get("amount_paid", entity.get("amount"))
    try:
        amount_inr = float(amount) / 100
    except (TypeError, ValueError):
        raise ValueError("Razorpay webhook amount is invalid") from None
    try:
        total_amount_inr = float(entity.get("amount")) / 100
    except (TypeError, ValueError):
        total_amount_inr = amount_inr
    if event_name == "payment.captured":
        amount_inr = total_amount_inr
    amount_field = "appointment_value" if action == "charge_fee" else "subscription_amount"
    payment_status = {
        "payment_link.paid": "recovered",
        "payment_link.partially_paid": "partially_paid",
        "payment.failed": "failed",
        "payment.captured": "recovered",
    }[event_name]
    return {
        "event_type": "payment_outcome",
        "event_name": event_name,
        "event_id": normalized_event_id,
        "webhook_event_id": normalized_event_id,
        "client_id": str(notes.get("client_id") or customer.get("email") or entity.get("customer_id") or "").strip() or None,
        "client_name": customer.get("name", "") or notes.get("client_name", ""),
        "client_email": customer.get("email", "") or notes.get("client_email", ""),
        "payment_id": entity.get("id") if entity_key == "payment" else entity.get("payment_id"),
        "payment_link_id": entity.get("id") if entity_key == "payment_link" else notes.get("payment_link_id"),
        "payment_status": payment_status,
        "recovery_action": action,
        "flexible_plan_id": str(notes.get("flexible_plan_id") or "").strip(),
        "flexible_plan_installment": str(notes.get("flexible_plan_installment") or "").strip(),
        "amount": amount_inr,
        "amount_paid": amount_inr,
        "amount_due": max(total_amount_inr - amount_inr, 0.0),
        "total_amount": total_amount_inr,
        amount_field: total_amount_inr,
        "validation_errors": [] if entity.get("id") and amount_inr > 0 else ["missing payment outcome fields"],
        "source": "razorpay_webhook",
    }


def credit_plan_installment(
    normalized: dict[str, Any],
    audit_path: Path = AUDIT_PATH,
    plan_path: Path | None = None,
) -> dict[str, Any] | None:
    """Credit one flexible-plan installment from a confirmed payment.

    Returns the credit result, or ``None`` when this payment does not belong to a
    plan. Called only after :func:`record_once` has accepted the delivery, so the
    provider's own retries cannot double-credit; a second, independent guard
    lives in ``plan_payment.payment_id``.

    Deliberately never raises. A plan we cannot credit must not stop the
    surrounding recovery record from being written — the money did arrive, and
    the ORIGINAL case must reflect it either way. Failures are audited instead.
    """
    from .flexible_plans import PLAN_DB_PATH, find_plan_for_payment, record_installment_payment

    store = plan_path or PLAN_DB_PATH
    try:
        plan = find_plan_for_payment(
            normalized.get("flexible_plan_id"),
            link_id=str(normalized.get("payment_link_id") or ""),
            path=store,
        )
    except Exception:  # noqa: BLE001 - a lookup failure is not a payment failure
        plan = None
    if plan is None:
        return None

    try:
        result = record_installment_payment(
            int(plan["id"]),
            payment_id=str(normalized.get("payment_id") or normalized.get("event_id") or ""),
            amount=normalized.get("amount_paid") or normalized.get("amount") or 0,
            link_id=str(normalized.get("payment_link_id") or ""),
            path=store,
        )
    except Exception as exc:  # noqa: BLE001 - audit it; never fail the webhook
        log_event(
            normalized, "flexible_plan_credit_failed", None, str(normalized.get("payment_status") or ""),
            audit_path, errors=[str(exc)], outcome="plan_credit_failed", actor="webhook_ingestion",
        )
        return None

    if result["duplicate"]:
        return result

    updated = result["plan"]
    installment = result["installment"] or {}
    log_event(
        {
            **normalized,
            "flexible_plan_id": str(updated["id"]),
            "flexible_plan_installment": str(installment.get("index") or ""),
            "plan_summary": updated["plan_summary"],
            "amount_paid": updated["amount_paid"],
            "amount_due": updated["amount_remaining"],
        },
        "flexible_plan_installment_paid",
        f"Installment {installment.get('index') or '?'} paid; {updated['plan_summary']}",
        "recovered" if result["completed"] else "partially_paid",
        audit_path,
        outcome="plan_completed" if result["completed"] else "payment_plan_active",
        actor="webhook_ingestion",
    )
    return result


def ingest_webhook(
    body: bytes | str,
    signature: str,
    secret: str,
    event_id: str,
    webhook_path: Path = WEBHOOK_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    recovery_path: Path = RECOVERY_DB_PATH,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    """Verify, deduplicate, normalize, and audit one Razorpay webhook delivery.

    The caller should pass Razorpay's ``x-razorpay-event-id`` header as
    ``event_id``. Duplicate deliveries are acknowledged without adding a second
    audit row, while invalid signatures are rejected before payload parsing.
    On a confirmed payment (payment_link.paid or payment.captured), a recovery
    record is written so the dashboard can show real recovered amounts.

    A payment whose notes carry ``flexible_plan_id`` also credits that
    installment, through the SAME recovery record and the same ``client_id``, so
    a plan payment settles the original case rather than opening a new one.
    """
    if not str(event_id or "").strip():
        raise ValueError("Razorpay webhook event_id is required")
    if not verify_signature(body, signature, secret):
        raise ValueError("Invalid Razorpay webhook signature")
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Razorpay webhook body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Razorpay webhook body must contain a JSON object")
    event_name = str(payload.get("event") or "")
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Razorpay webhook event: {event_name or 'missing event'}")

    # Failure deliveries open a recovery case. They are normalized to the same
    # RevenueEvent contract used by CSV backfill before being appended to audit.
    if event_name in RAZORPAY_FAILURE_EVENTS:
        normalized = from_razorpay_webhook(payload, event_id)
        if not record_once(event_id, event_name, payload, webhook_path):
            return {"duplicate": True, "event_id": event_id, "event": normalized}
        row = log_event(normalized, "detected", None, "failed", audit_path, outcome="case_opened", actor="webhook_ingestion")
        return {"duplicate": False, "event_id": event_id, "event": normalized, "audit": row}

    # payment.authorized is evidence of progress, not settled revenue. Keep it
    # visible without incrementing recovered rupees.
    if event_name == "payment.authorized":
        entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
        notes = entity.get("notes") if isinstance(entity.get("notes"), dict) else {}
        normalized = {
            "event_type": "payment_outcome", "event_name": event_name, "event_id": event_id,
            "client_id": notes.get("client_id") or entity.get("customer_id"),
            "client_name": notes.get("client_name", ""), "client_email": notes.get("client_email", ""),
            "payment_id": entity.get("id"), "subscription_id": entity.get("subscription_id", ""),
            "payment_status": "authorized", "recovery_action": "retry_payment",
            "amount": float(entity.get("amount") or 0) / 100, "validation_errors": [],
            "source": "razorpay_webhook",
        }
        if not record_once(event_id, event_name, payload, webhook_path):
            return {"duplicate": True, "event_id": event_id, "event": normalized}
        row = log_event(normalized, "payment_authorized", None, "authorized", audit_path, outcome="awaiting_capture", actor="webhook_ingestion")
        return {"duplicate": False, "event_id": event_id, "event": normalized, "audit": row}

    normalized = normalize_webhook(payload, event_id=event_id)
    if not record_once(event_id, event_name, payload, webhook_path):
        return {"duplicate": True, "event_id": event_id, "event": normalized}
    row = log_event(normalized, normalized["recovery_action"], None, normalized["payment_status"], audit_path, actor="webhook_ingestion")
    plan_credit: dict[str, Any] | None = None
    # Write a durable recovery record so the dashboard shows confirmed amounts.
    if normalized.get("payment_status") == "recovered" and normalized.get("client_id"):
        # An installment payment is credited to its plan FIRST, then falls through
        # to the ordinary recovery record below. Both use the same client_id, which
        # is what keeps a plan payment attributed to the original voice case
        # instead of opening a disconnected one.
        plan_credit = credit_plan_installment(normalized, audit_path=audit_path, plan_path=plan_path)
        # Attribution is decided HERE, once, at the moment payment is confirmed:
        # the newest call attempt is compared against the newest confirmed email
        # send, and whichever acted last gets the credit. Both the winning channel
        # and the instant it acted are handed to write_recovery_record so they land
        # in the same INSERT as the amount and recovered_at. Nothing recomputes
        # this later, which is what lets "Avg time to payment" be a subtraction of
        # two columns on one row instead of a join back into the call log.
        #
        # Imported inside the function on purpose: voice_calls reads recovery rows
        # back out of this module, so a module-level import would close that loop.
        from .voice_calls import attribute_recovery

        recovered_via, recovery_triggered_at = attribute_recovery(
            str(normalized["client_id"]), audit_path=audit_path
        )
        write_recovery_record(
            client_id=str(normalized["client_id"]),
            amount_recovered=float(normalized.get("amount_paid") or normalized.get("amount") or 0),
            payment_link_id=normalized.get("payment_link_id"),
            event_id=str(event_id),
            event_name=str(payload.get("event") or ""),
            path=recovery_path,
            recovered_via=recovered_via,
            recovery_triggered_at=recovery_triggered_at,
        )
    return {"duplicate": False, "event_id": event_id, "event": normalized, "audit": row, "plan_credit": plan_credit}


def simulate_paid_webhook(
    client_id: str,
    amount_inr: float,
    client_name: str = "",
    client_email: str = "",
    recovery_action: str = "retry_payment",
    secret: str | None = None,
    webhook_path: Path = WEBHOOK_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    recovery_path: Path = RECOVERY_DB_PATH,
) -> dict[str, Any]:
    """Construct, sign, and ingest a ``payment_link.paid`` delivery locally.

    This drives the *same* verified ingestion path used in production, so the
    seeded recovery is indistinguishable from a real one: it writes a durable
    recovery record and an audit row. It exists for environments where the
    Razorpay Test Mode payment-link cap blocks minting real links (and thus real
    webhooks), so the dashboard can still show a confirmed settlement. The
    payload is signed with the configured webhook secret and rejected by
    ``ingest_webhook`` if that secret is wrong, exactly like a live delivery.
    """
    import os
    import uuid

    resolved_secret = secret if secret is not None else os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not resolved_secret:
        raise ValueError("RAZORPAY_WEBHOOK_SECRET must be set to simulate a webhook delivery")
    if recovery_action not in {"charge_fee", "retry_payment", "resend_payment_link"}:
        recovery_action = "retry_payment"
    if not str(client_id or "").strip():
        raise ValueError("A client_id is required to simulate a recovery")
    amount_paise = int(round(float(amount_inr) * 100))
    if amount_paise <= 0:
        raise ValueError("Simulated recovery amount must be a positive number")
    event_id = f"sim_{uuid.uuid4().hex}"
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": f"plink_sim_{uuid.uuid4().hex[:14]}",
                    "amount": amount_paise,
                    "amount_paid": amount_paise,
                    "customer": {"name": client_name, "email": client_email},
                    "notes": {
                        "client_id": str(client_id),
                        "client_name": client_name,
                        "client_email": client_email,
                        "recovery_action": recovery_action,
                    },
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(resolved_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return ingest_webhook(
        body,
        signature,
        resolved_secret,
        event_id,
        webhook_path=webhook_path,
        audit_path=audit_path,
        recovery_path=recovery_path,
    )
