"""Shared application service facade for scheduler and dashboard operations."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from .audit_log import AUDIT_PATH, log_event
from .attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH, check_cooldown, get_next_retry_at, list_client_email_statuses, list_owner_flags, record_client_email_sent, resolve_owner_flag
from .razorpay_webhooks import RECOVERY_DB_PATH, list_recovery_records
from .waitlist import DB_PATH as WAITLIST_DB_PATH, add_to_waitlist, get_next_in_line, list_waitlist, mark_slot, update_waitlist_entry


MESSAGE_ACTIONS = {"charge_fee", "retry_payment", "offer_waitlist", "friendly_reminder"}
CASE_ACTIONS = MESSAGE_ACTIONS | {"escalate_human"}


def case_key(event: dict[str, Any], action: str) -> str:
    """Return a stable identity for a case, independent of processing time."""
    ignored = {"validation_errors", "waitlist_entry_exists", "short_url", "payment_link_id", "message", "delivery", "invoice_number", "invoice_status", "invoice_due_date", "invoice_amount", "invoice_filename"}
    stable_event = {key: value for key, value in event.items() if key not in ignored}
    payload = json.dumps({"action": action, "event": stable_event}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def draft_invoice_number(key: str, timestamp: str) -> str:
    """Return a stable provisional invoice number for a case with no issued invoice.

    A real number is only minted at send time by `build_invoice`, which hashes the
    Razorpay payment link and stamps the current date, so it cannot be reproduced
    for an unsent case. This derives a placeholder from the case identity and the
    case's own newest audit timestamp instead, so the dashboard always has a
    stable reference to show and the same case never changes number between reads.
    """
    digest = hashlib.sha256(f"draft:{key}".encode("utf-8")).hexdigest()[:8].upper()
    day = "".join(character for character in str(timestamp)[:10] if character.isdigit()) or "00000000"
    return f"INV-{day}-{digest}"


class RecoveryService:
    """Coordinate operational state without coupling callers to storage details."""

    def __init__(self, audit_path: Path = AUDIT_PATH, attempts_path: Path = ATTEMPTS_DB_PATH, waitlist_path: Path = WAITLIST_DB_PATH, recovery_path: Path = RECOVERY_DB_PATH):
        self.audit_path = audit_path
        self.attempts_path = attempts_path
        self.waitlist_path = waitlist_path
        self.recovery_path = recovery_path

    def review_flags(self) -> list[dict[str, Any]]:
        return list_owner_flags(self.attempts_path)

    def resolve_review(self, flag_id: int) -> bool:
        return resolve_owner_flag(flag_id, self.attempts_path)

    def add_waitlist_client(self, client: dict[str, Any]) -> dict[str, Any]:
        return add_to_waitlist(client, self.waitlist_path)

    def list_waitlist(self) -> list[dict[str, Any]]:
        return list_waitlist(self.waitlist_path)

    def update_waitlist_client(self, entry_id: int, client: dict[str, Any]) -> dict[str, Any]:
        return update_waitlist_entry(entry_id, client, self.waitlist_path)

    def next_waitlist_client(self) -> dict[str, Any] | None:
        return get_next_in_line(self.waitlist_path)

    def set_slot_status(self, status: str) -> str:
        return mark_slot(status, self.waitlist_path)

    def list_clients(self) -> list[dict[str, Any]]:
        """Return current business cases with payment outcomes and audit trails."""
        if not self.audit_path.exists():
            return []
        with self.audit_path.open(newline="", encoding="utf-8") as handle:
            audit_rows = list(csv.DictReader(handle))

        grouped: dict[str, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
        for index, row in enumerate(audit_rows):
            client_id = str(row.get("client_id") or "").strip()
            action = str(row.get("action") or "").strip()
            if not client_id or client_id.lower() == "unknown" or action not in CASE_ACTIONS:
                continue
            try:
                event = json.loads(row.get("event_json") or "{}")
                if not isinstance(event, dict):
                    event = {}
            except json.JSONDecodeError:
                event = {}
            name = str(row.get("client_name") or event.get("client_name") or "").strip()
            if not name:
                continue
            grouped.setdefault(client_id, []).append((index, row, event))

        statuses = list_client_email_statuses(self.attempts_path)
        # Webhook-confirmed recoveries — the source of truth for ₹ recovered.
        recovery_records = list_recovery_records(self.recovery_path)
        clients = []
        for client_id, entries in grouped.items():
            entries.sort(key=lambda item: (str(item[1].get("timestamp") or ""), item[0]))
            _, row, event = entries[-1]
            condition = str(row.get("action") or "escalate_human")
            key = case_key(event, condition)
            last_activity_at = str(row.get("timestamp") or "") or None
            issued_invoice_number = next(
                (
                    str(audit_event.get("invoice_number")).strip()
                    for _, _, audit_event in reversed(entries)
                    if str(audit_event.get("invoice_number") or "").strip()
                ),
                None,
            )
            invoice_number = issued_invoice_number or draft_invoice_number(key, last_activity_at or "")
            status = statuses.get(client_id)
            sent = bool(status and status.get("current_condition") == condition and status.get("current_case_key") == key)
            audit_trail = [
                {
                    "timestamp": audit_row.get("timestamp") or "",
                    "action": audit_row.get("action") or "escalate_human",
                    "payment_status": audit_row.get("payment_status") or "not_applicable",
                    "outcome": audit_row.get("outcome") or "",
                    "status": audit_row.get("status") or "",
                    "errors": audit_row.get("errors") or "",
                    "invoice_number": audit_event.get("invoice_number") or "",
                }
                for _, audit_row, audit_event in entries
            ]
            # Merge webhook-confirmed recovery — overrides link_created status.
            recovery = recovery_records.get(client_id)
            confirmed_payment_status = row.get("payment_status") or "not_applicable"
            amount_recovered: float | None = None
            recovered_at: str | None = None
            if recovery:
                confirmed_payment_status = "recovered"
                amount_recovered = float(recovery.get("amount_recovered") or 0)
                recovered_at = recovery.get("recovered_at")
            # Cooldown and next-retry window for the UI stopping-rule card.
            cooldown_active = check_cooldown(client_id, self.attempts_path, action_scope="payment")
            next_retry_at = get_next_retry_at(client_id, self.attempts_path, action_scope="payment") if cooldown_active else None
            clients.append({
                "client_id": client_id,
                "name": row.get("client_name") or event.get("client_name"),
                "email": event.get("client_email") or "",
                "condition": condition,
                "email_sent": sent,
                "last_email_sent_at": status.get("last_email_sent_at") if sent and status else None,
                "last_activity_at": last_activity_at,
                "can_send": condition in MESSAGE_ACTIONS and "@" in str(event.get("client_email") or ""),
                "case_key": key,
                "case": event,
                "payment_status": confirmed_payment_status,
                "outcome": row.get("outcome") or "",
                "invoice_number": invoice_number,
                "invoice_status": event.get("invoice_status"),
                "invoice_due_date": event.get("invoice_due_date"),
                "invoice_amount": event.get("invoice_amount"),
                "invoice_filename": event.get("invoice_filename"),
                "audit_trail": audit_trail,
                # Webhook-confirmed recovery fields.
                "amount_recovered": amount_recovered,
                "recovered_at": recovered_at,
                # Stopping-rule fields for the UI drawer.
                "cooldown_active": cooldown_active,
                "next_retry_at": next_retry_at,
            })
        return sorted(clients, key=lambda item: str(item["name"]).lower())

    def send_client_email(self, client_id: str, resend: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Generate and deliver one client's current-case email, then persist success."""
        client = next((item for item in self.list_clients() if item["client_id"] == str(client_id)), None)
        if client is None:
            raise LookupError("Client not found")
        if not client["can_send"]:
            raise ValueError("The current case does not have a sendable client email action")
        if client["email_sent"] and not resend:
            raise ValueError("Email has already been sent for this case")
        from .handlers import handle_action

        handled = handle_action(client["case"], client["condition"], deliver=True, **kwargs)
        status = record_client_email_sent(client["client_id"], client["condition"], handled["message"], self.attempts_path, client["case_key"])
        if handled.get("invoice_number"):
            log_event(handled, client["condition"], f"Invoice {handled['invoice_number']} generated and sent", "link_created", self.audit_path, outcome="invoice_sent")
        return {
            **client,
            "email_sent": True,
            "last_email_sent_at": status["last_email_sent_at"],
            "last_message": handled["message"],
            "delivery": handled.get("delivery"),
            "invoice_number": handled.get("invoice_number"),
            "invoice_status": handled.get("invoice_status"),
            "invoice_due_date": handled.get("invoice_due_date"),
            "invoice_amount": handled.get("invoice_amount"),
            "invoice_filename": handled.get("invoice_filename"),
        }

    def process_event(self, event: dict[str, Any], live: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Process one event through the shared scheduler/dashboard service path."""
        from batch_runner import run_event

        return run_event(
            event,
            attempts_path=self.attempts_path,
            audit_path=self.audit_path,
            waitlist_path=self.waitlist_path,
            live=live,
            **kwargs,
        )

    def retry_event(self, event: dict[str, Any], live: bool = True) -> dict[str, Any]:
        """Execute an owner-approved retry through the same scheduler path."""
        return self.process_event(event, live=live)

    def acknowledge_owner_action(self, flag_id: int, actor: str = "dashboard") -> bool:
        resolved = resolve_owner_flag(flag_id, self.attempts_path)
        if resolved:
            self.record_system_event({"event_type": "owner_acknowledgement", "client_id": str(flag_id), "source": actor}, "acknowledge_owner", f"Owner flag {flag_id} acknowledged")
        return resolved

    def record_system_event(self, event: dict[str, Any], action: str, reason: str) -> dict[str, str]:
        return log_event(event, action, None, "not_applicable", self.audit_path, errors=[reason], outcome="system_action")
