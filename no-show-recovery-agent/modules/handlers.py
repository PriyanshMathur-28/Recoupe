"""Validated action handlers for messages, payment links, and delivery."""
from __future__ import annotations

from typing import Any, Callable

from .invoices import build_invoice
from .message_generator import generate_message
from .messenger import send_message
from .payments import create_payment_link


def handle_action(event: dict[str, Any], action: str, payment_client: Any = None, llm_call: Callable[[str], str] | None = None, message_service: Any = None, deliver: bool = False) -> dict[str, Any]:
    """Apply an action and optionally deliver the resulting customer message."""
    if event.get("validation_errors"):
        raise ValueError("Cannot handle an event with validation errors: " + "; ".join(event["validation_errors"]))
    result = dict(event)
    if action in {"charge_fee", "retry_payment"}:
        amount = event.get("fee_amount", event.get("appointment_value", event.get("subscription_amount")))
        if amount is None:
            raise ValueError(f"{action} requires an amount")
        contact = event.get("client_phone") or event.get("client_email")
        if not str(contact or "").strip():
            raise ValueError(f"{action} requires a client phone or email")
        payment = create_payment_link(amount, event.get("client_name") or "Client", action.replace("_", " ").title(), contact, client=payment_client)
        result["payment_link_id"] = payment["id"]
        result["short_url"] = payment["short_url"]
        invoice = build_invoice(result, action, payment["short_url"])
        result.update({key: value for key, value in invoice.items() if key != "invoice_pdf"})
        result["_invoice_attachment"] = {"content": invoice["invoice_pdf"], "filename": invoice["invoice_filename"]}
    result["message"] = generate_message(result, action, llm=llm_call)
    if deliver:
        contact = event.get("client_email")
        if not str(contact or "").strip() or "@" not in str(contact):
            raise ValueError(f"{action} requires a valid client email for delivery")
        result["delivery"] = send_message(contact, action.replace("_", " ").title(), result["message"], result.get("short_url"), service=message_service, attachment=result.get("_invoice_attachment"))
    result.pop("_invoice_attachment", None)
    return result
