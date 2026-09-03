"""Validated action handlers for messages, payment links, and delivery.

This is layer 5, the bounded action executor. It only knows how to perform the
actions in ``EXECUTABLE_ACTIONS``; anything else raises. It never decides
*whether* an action should run — ``modules.policy_engine.evaluate`` owns that —
so an unapproved action can never reach a customer through this path.
"""
from __future__ import annotations

from typing import Any, Callable

from .invoices import build_invoice
from .message_generator import generate_message
from .messenger import send_message
from .payments import PaymentLinkLimitError, create_payment_link

# Actions that mint a Razorpay payment link before drafting the message.
PAYMENT_LINK_ACTIONS = frozenset({"charge_fee", "retry_payment", "resend_payment_link"})
# Actions that are message-only: the ladder steps and the waitlist offer.
MESSAGE_ONLY_ACTIONS = frozenset({"friendly_reminder", "firm_reminder", "final_notice", "offer_waitlist"})
EXECUTABLE_ACTIONS = PAYMENT_LINK_ACTIONS | MESSAGE_ONLY_ACTIONS


def handle_action(event: dict[str, Any], action: str, payment_client: Any = None, llm_call: Callable[[str], str] | None = None, message_service: Any = None, deliver: bool = False) -> dict[str, Any]:
    """Apply an approved action and optionally deliver the resulting message."""
    if event.get("validation_errors"):
        raise ValueError("Cannot handle an event with validation errors: " + "; ".join(event["validation_errors"]))
    if action not in EXECUTABLE_ACTIONS:
        raise ValueError(f"{action} is not an executable action")
    result = dict(event)
    if action in PAYMENT_LINK_ACTIONS:
        amount = event.get("amount", event.get("fee_amount", event.get("appointment_value", event.get("subscription_amount"))))
        if amount is None:
            raise ValueError(f"{action} requires an amount")
        contact = event.get("client_phone") or event.get("client_email")
        if not str(contact or "").strip():
            raise ValueError(f"{action} requires a client phone or email")
        try:
            payment = create_payment_link(amount, event.get("client_name") or "Client", action.replace("_", " ").title(), contact, client=payment_client)
        except PaymentLinkLimitError as exc:
            # The provider is out of link budget (e.g. Razorpay Test Mode's
            # lifetime cap). This is recoverable: send the recovery message
            # without a fresh link rather than failing the whole delivery. The
            # message generator falls back to generic payment-link wording, and
            # the degradation is surfaced so the caller/audit can record it.
            result["payment_link_unavailable"] = True
            result["payment_link_note"] = str(exc)
        else:
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
