"""LLM-backed recovery message generation with resilient provider handling."""
from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

# Language that must never reach a customer, regardless of what the model
# returns. This is our own non-harassment standard for commercial receivables
# collection; see README "Compliance posture" for why no RBI claim is made.
BANNED_PHRASES = (
    "legal action",
    "lawyer",
    "police",
    "court",
    "blacklist",
    "defaulter",
    "recovery agent",
    "credit score",
    "seize",
    "criminal",
    "consequences will",
    "last warning",
)

TEMPLATES = {
    "charge_fee": """Write a warm, concise client message (under 80 words) about a late cancellation fee. Client: {client_name}. Appointment: {appointment_datetime}. Fee amount: {amount}. Payment link: {payment_link}. Explain that the fee helps protect reserved appointment time and invite them to reply with questions. Do not sound threatening or robotic. Return only the message.""",
    "offer_waitlist": """Write a warm, concise client message (under 80 words) offering the newly available appointment to a waitlist client. Client: {client_name}. Appointment: {appointment_datetime}. Ask them to reply to confirm interest. Return only the message.""",
    "friendly_reminder": """Write a warm, concise client message (under 80 words) acknowledging a client's first cancellation. Client: {client_name}. Appointment: {appointment_datetime}. Be understanding, avoid blame or fees, and invite them to reschedule. Return only the message.""",
    "retry_payment": """Write a warm, concise client message (under 80 words) explaining that a subscription payment could not be completed. Client: {client_name}. Reason: {failure_reason}. Payment link: {payment_link}. Ask them to update their payment method and avoid exposing sensitive payment details. Return only the message.""",
    # Ladder step 1b: the payment instrument itself is the problem, so the
    # remedy is a fresh link rather than another charge on the same method.
    "resend_payment_link": """Write a warm, concise client message (under 80 words) explaining that the saved payment method could not be charged and a fresh secure payment link is ready. Client: {client_name}. Reason: {failure_reason}. Payment link: {payment_link}. Ask them to complete the payment with any card or UPI method. Never ask for card numbers, CVV, OTP, or any credential. Return only the message.""",
    # Ladder step 2: same facts, firmer tone, still no threat of any kind.
    "firm_reminder": """Write a direct but professional client message (under 90 words) about an outstanding payment that is now overdue. Client: {client_name}. Reason: {failure_reason}. Payment link: {payment_link}. Days outstanding: {aging_days}. State clearly that the amount is due and ask them to settle it or reply with a date they can pay. Be businesslike, not warm and not threatening. Never mention legal action, credit scores, recovery agents, or consequences. Return only the message.""",
    # Ladder step 3: final automated contact before human handoff.
    "final_notice": """Write a formal, factual client message (under 90 words) stating this is the final automated reminder for an outstanding payment before the account is passed to a human account manager. Client: {client_name}. Reason: {failure_reason}. Payment link: {payment_link}. Days outstanding: {aging_days}. Invite them to pay or to reply and discuss the account. Never threaten legal action, penalties, credit reporting, or third-party recovery. Return only the message.""",
}


def _reject_banned_language(message: str, action: str) -> str:
    """Block any generated message containing harassment-adjacent language.

    The model is a drafting tool, not the final authority on tone. A firm or
    final-notice prompt is exactly where an LLM is most likely to escalate into
    threats, so the output is checked before it can ever reach delivery.
    """
    lowered = message.lower()
    hits = [phrase for phrase in BANNED_PHRASES if phrase in lowered]
    if hits:
        raise ValueError(f"{action} message rejected by language filter: {', '.join(hits)}")
    return message


def _groq_text(payload: Any) -> str:
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Groq returned an invalid response format") from exc
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Groq returned an empty message")
    return text.strip()


def _gemini_text(payload: Any) -> str:
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini returned an invalid response format") from exc
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Gemini returned an empty message")
    return text.strip()


def call_llm(prompt: str) -> str:
    """Try configured providers in order, falling back on provider failures."""
    load_dotenv()
    errors: list[str] = []
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if groq_key:
        try:
            response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {groq_key}"}, json={"model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"), "messages": [{"role": "user", "content": prompt}], "temperature": 0.4}, timeout=30)
            response.raise_for_status()
            return _groq_text(response.json())
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            errors.append(f"Groq: {exc}")
    if gemini_key:
        try:
            response = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}", json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
            response.raise_for_status()
            return _gemini_text(response.json())
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            errors.append(f"Gemini: {exc}")
    if errors:
        raise RuntimeError("All configured LLM providers failed: " + " | ".join(errors))
    raise RuntimeError("Set GROQ_API_KEY or GEMINI_API_KEY in .env first.")


def generate_message(event: dict[str, Any], action: str, llm: Any = None) -> str:
    """Render the action template, call the LLM, and filter the result."""
    if action not in TEMPLATES:
        raise ValueError(f"Unsupported message action: {action}")
    prompt = TEMPLATES[action].format(
        client_name=event.get("client_name", "there"), appointment_datetime=event.get("appointment_datetime", "the appointment"),
        amount=event.get("fee_amount", event.get("appointment_value", event.get("subscription_amount", "the stated amount"))),
        failure_reason=event.get("failure_reason", "the recent payment attempt"), payment_link=event.get("short_url", "the payment link provided by our team"),
        aging_days=event.get("aging_days", "several"),
    )
    message = llm(prompt) if llm is not None else call_llm(prompt)
    return _reject_banned_language(str(message).strip(), action)
