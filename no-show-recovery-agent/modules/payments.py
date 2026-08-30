"""Razorpay Test Mode payment-link integration."""
from __future__ import annotations

import math
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from dotenv import load_dotenv
import razorpay


class PaymentLinkProviderError(RuntimeError):
    """A safe, actionable failure returned by the payment-link provider."""


class PaymentLinkLimitError(PaymentLinkProviderError):
    """The provider cannot mint any more links (e.g. Razorpay Test Mode cap).

    This is a *recoverable* condition: callers are expected to degrade to a
    message-only send instead of failing the whole delivery, so it is a distinct
    subclass a caller can catch specifically while still treating every other
    provider failure as a hard error.
    """


def _amount_in_paise(amount: Any) -> int:
    """Convert a positive INR amount using explicit two-decimal currency rounding."""
    if isinstance(amount, bool):
        raise ValueError("Payment-link amount must be a finite positive number.")
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Payment-link amount must be a finite positive number.") from None
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        raise ValueError("Payment-link amount must be a finite positive number.")
    paise = (decimal_amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if paise <= 0:
        raise ValueError("Payment-link amount rounds to zero paise.")
    return int(paise)


def create_payment_link(amount: Any, name: str, description: str, contact: str, client: Any = None) -> dict[str, Any]:
    """Create a Razorpay payment link; accept either a phone or an email contact."""
    load_dotenv()
    normalized_contact = str(contact or "").strip()
    if not normalized_contact:
        raise ValueError("Payment-link contact is required.")
    amount_paise = _amount_in_paise(amount)
    if client is None:
        key_id = os.getenv("RAZORPAY_KEY_ID")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env first.")
        client = razorpay.Client(auth=(key_id, key_secret))
    customer = {"name": name}
    if "@" in normalized_contact:
        customer["email"] = normalized_contact
    else:
        customer["contact"] = normalized_contact
    try:
        response = client.payment_link.create({
            "amount": amount_paise,
            "currency": "INR",
            "description": description,
            "customer": customer,
            "notify": {"sms": False, "email": False},
            "reminder_enable": True,
        })
    except razorpay.errors.ServerError as exc:
        provider_message = str(exc).strip()
        if "test mode limit" in provider_message.lower() and "payment_link" in provider_message.lower():
            raise PaymentLinkLimitError(
                "Razorpay Test Mode has reached its payment-link limit. Cancel old test payment links "
                "in the Razorpay dashboard or use a fresh test account to mint new links."
            ) from exc
        raise PaymentLinkProviderError(
            "Razorpay could not create the payment link. Try again later; no email was sent."
        ) from exc
    except razorpay.errors.BadRequestError as exc:
        raise PaymentLinkProviderError(
            f"Razorpay rejected the payment-link request: {exc}. No email was sent."
        ) from exc
    if not isinstance(response, dict) or not response.get("id") or not response.get("short_url"):
        raise RuntimeError("Razorpay returned an incomplete payment-link response.")
    return response
