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

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_DB_PATH = ROOT / "data" / "webhook_events.sqlite3"
SUPPORTED_EVENTS = {"payment_link.paid", "payment_link.partially_paid", "payment.failed"}


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


def normalize_webhook(payload: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
    """Map a verified Razorpay webhook payload to a recovery event.

    ``event_id`` is Razorpay's delivery identity and is deliberately kept
    separate from the payment-link and customer identifiers in the payload.
    """
    event_name = str(payload.get("event") or "")
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        raise ValueError("Razorpay webhook event_id is required")
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Razorpay webhook event: {event_name or 'missing event'}")
    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    if not isinstance(entity, dict):
        raise ValueError("Razorpay webhook payment_link entity is missing")
    customer = entity.get("customer") or {}
    notes = entity.get("notes") or {}
    action = str(notes.get("recovery_action") or "retry_payment")
    if action not in {"charge_fee", "retry_payment"}:
        raise ValueError("Webhook recovery_action must be charge_fee or retry_payment")
    amount = entity.get("amount_paid", entity.get("amount"))
    try:
        amount_inr = float(amount) / 100
    except (TypeError, ValueError):
        raise ValueError("Razorpay webhook amount is invalid") from None
    try:
        total_amount_inr = float(entity.get("amount")) / 100
    except (TypeError, ValueError):
        total_amount_inr = amount_inr
    amount_field = "appointment_value" if action == "charge_fee" else "subscription_amount"
    payment_status = {
        "payment_link.paid": "recovered",
        "payment_link.partially_paid": "partially_paid",
        "payment.failed": "failed",
    }[event_name]
    return {
        "event_type": "payment_outcome",
        "event_name": event_name,
        "event_id": normalized_event_id,
        "webhook_event_id": normalized_event_id,
        "client_id": str(notes.get("client_id") or customer.get("email") or "").strip() or None,
        "client_name": customer.get("name", ""),
        "client_email": customer.get("email", ""),
        "payment_link_id": entity.get("id"),
        "payment_status": payment_status,
        "recovery_action": action,
        "amount": amount_inr,
        "amount_paid": amount_inr,
        "amount_due": max(total_amount_inr - amount_inr, 0.0),
        "total_amount": total_amount_inr,
        amount_field: total_amount_inr,
        "validation_errors": [] if entity.get("id") and amount_inr > 0 else ["missing payment-link outcome fields"],
        "source": "razorpay_webhook",
    }


def ingest_webhook(
    body: bytes | str,
    signature: str,
    secret: str,
    event_id: str,
    webhook_path: Path = WEBHOOK_DB_PATH,
    audit_path: Path = AUDIT_PATH,
) -> dict[str, Any]:
    """Verify, deduplicate, normalize, and audit one Razorpay webhook delivery.

    The caller should pass Razorpay's ``x-razorpay-event-id`` header as
    ``event_id``. Duplicate deliveries are acknowledged without adding a second
    audit row, while invalid signatures are rejected before payload parsing.
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
    normalized = normalize_webhook(payload, event_id=event_id)
    if not record_once(event_id, str(payload.get("event") or ""), payload, webhook_path):
        return {"duplicate": True, "event_id": event_id, "event": normalized}
    row = log_event(normalized, normalized["recovery_action"], None, normalized["payment_status"], audit_path)
    return {"duplicate": False, "event_id": event_id, "event": normalized, "audit": row}
