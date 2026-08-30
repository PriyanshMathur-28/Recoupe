"""Vapi boundary: browser web calls, optional outbound calls, webhook handling.

Everything provider-specific lives here. The rest of the codebase talks about
"a call was placed" and "a call ended with this outcome"; only this module knows
that Vapi exists, what an ``endedReason`` is, or which key the browser may hold.

The flow this project uses
--------------------------
::

    operator's laptop browser
        -> Vapi web call (public key, mic + speakers, no phone number)
            -> Vapi's AI agent talks to the person
                -> transcript comes back to this backend
                    -> two-step outcome rule -> call_log row closed

A web call needs only ``VAPI_PUBLIC_KEY``. The public key is designed to be
shipped to a browser; the private key never is, and this module never puts it in
a response body. Outbound telephony (:func:`place_call`) is kept as a second
path for later, but nothing in the web flow requires it.

Two closing paths, one rule
---------------------------
A web call can be closed from either direction:

* the browser reports the call ended and hands over the transcript
  (:func:`complete_web_call`), or
* Vapi's server sends an ``end-of-call-report`` to ``POST /webhooks/vapi``
  (:func:`normalize_end_of_call`).

Both funnel into :func:`modules.voice_calls.resolve_call_outcome`, so both run
the identical two steps:

    step 1  answered?  -> no  => outcome = "no_answer", classification skipped
                       -> yes => step 2
    step 2  the captured speech goes through the SAME typed-JSON 4-way
            classification, which may only return an ANSWERED outcome.

Whichever path arrives first wins; the other finds the row already closed and
reports a duplicate. That is enforced by ``close_call``'s ``WHERE ended_at = ''``
guard, not by ordering luck.

No simulated path
-----------------
There is deliberately no demo, sample or simulated call. A ``call_log`` row is
only ever written for a call the browser genuinely opened against Vapi, and an
outcome is only ever written from speech Vapi genuinely captured. When
``VAPI_PUBLIC_KEY`` is absent the feature refuses to start a call
(:class:`VapiConfigError`) instead of fabricating one, because a fabricated
transcript would be scored by the real classifier and inflate the real metric
cards.

The follow-up email
-------------------
After an answered call is classified, :func:`complete_web_call` asks an LLM
whether the conversation warrants sending the payment link. That decision can
only ever be acted on for a ``promised_to_pay`` outcome — ``declined`` and
``escalated`` never send, whatever the model says. The send is executed as the
``resend_payment_link`` action so a real Razorpay link and invoice are produced,
matching what the voice agent promises aloud.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Callable

from .audit_log import AUDIT_PATH
from .voice_calls import (
    VOICE_DB_PATH,
    answered_from_ended_reason,
    attach_provider_call_id,
    close_call,
    find_call_by_provider_id,
    get_call,
    open_call,
    record_call_audit,
    resolve_call_outcome,
)

VAPI_API_BASE = "https://api.vapi.ai"

# Vapi delivers its shared secret in a plain header by default. A signature
# header is also accepted for deployments that front Vapi with a signer.
SECRET_HEADER = "X-Vapi-Secret"
SIGNATURE_HEADER = "X-Vapi-Signature"

# The silence window that decides step 1. If no speech was captured within this
# many seconds of the call connecting, nobody engaged. This is the *only* thing
# the window decides — it never touches what the speech meant.
SILENCE_WINDOW_SECONDS = 5.0

DEFAULT_FIRST_MESSAGE = (
    "Hello, this is the accounts team calling about an outstanding balance from your "
    "recent appointment. Is now an alright time to talk about it?"
)

ASSISTANT_SYSTEM_PROMPT = """You are a calm, courteous accounts-recovery voice agent for a small clinic.

Your only job on this call is to find out whether the client intends to pay the
outstanding balance, and if so, roughly when. You have no authority to change the
amount, waive it, offer a discount, or agree to a payment plan.

Rules you never break:
- State the amount only if the client asks, and only the figure you were given.
- Never threaten, never imply legal action, never raise your voice.
- If the client is upset, disputes the charge, asks for a manager, mentions a
  lawyer, or says anything you are unsure how to handle: apologise briefly, say a
  member of the team will follow up personally, and end the call politely.
- If the client agrees to pay, confirm the day out loud once, tell them a payment
  link will arrive by email, thank them, and end the call.
- If the client declines, thank them for their time and end the call. Do not
  argue and do not ask a second time.
- Keep the whole call under two minutes.
"""


class VapiConfigError(RuntimeError):
    """Raised when a call is requested but Vapi is not configured for it."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def vapi_config() -> dict[str, Any]:
    """Read the Vapi environment into one dict, with computed readiness flags.

    ``web_ready`` is what the browser flow checks and needs only the public key.
    ``phone_ready`` gates the optional outbound path. Splitting them means a
    missing phone number can never block the flow this project actually uses.
    """
    public_key = (os.getenv("VAPI_PUBLIC_KEY") or "").strip()
    private_key = (os.getenv("VAPI_PRIVATE_KEY") or "").strip()
    phone_number_id = (os.getenv("VAPI_PHONE_NUMBER_ID") or "").strip()
    return {
        "public_key": public_key,
        "private_key": private_key,
        "phone_number_id": phone_number_id,
        "assistant_id": (os.getenv("VAPI_ASSISTANT_ID") or "").strip(),
        "webhook_secret": (os.getenv("VAPI_WEBHOOK_SECRET") or "").strip(),
        "voice_id": (os.getenv("VAPI_VOICE_ID") or "").strip(),
        "auto_email": _flag("VOICE_AUTO_EMAIL", default=True),
        "web_ready": bool(public_key),
        "phone_ready": bool(private_key and phone_number_id),
    }


def resolve_mode(config: dict[str, Any] | None = None) -> str:
    """Return the transport for the next call: 'web', or 'unconfigured'.

    There is no fallback that fakes a call. A missing ``VAPI_PUBLIC_KEY`` is
    reported as ``unconfigured`` so the dashboard can say so plainly, and
    :func:`start_web_call` refuses rather than inventing a conversation.
    """
    settings = config or vapi_config()
    return "web" if settings["web_ready"] else "unconfigured"


def config_status() -> dict[str, Any]:
    """Describe Vapi readiness for the dashboard, without leaking any secret."""
    settings = vapi_config()
    return {
        "mode": resolve_mode(settings),
        "web_ready": settings["web_ready"],
        "phone_ready": settings["phone_ready"],
        "auto_email": settings["auto_email"],
        "has_public_key": bool(settings["public_key"]),
        "has_private_key": bool(settings["private_key"]),
        "has_assistant": bool(settings["assistant_id"]),
        "has_webhook_secret": bool(settings["webhook_secret"]),
        "silence_window_seconds": SILENCE_WINDOW_SECONDS,
    }


# ---------------------------------------------------------------------------
# Assistant definition — shared by the web and phone paths
# ---------------------------------------------------------------------------


def build_assistant(
    settings: dict[str, Any],
    *,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
) -> dict[str, Any]:
    """Return either an assistant reference or a full transient assistant.

    Keeping the inline branch is what makes ``VAPI_ASSISTANT_ID`` optional: the
    call works with nothing configured in the Vapi dashboard, and an operator who
    later builds an assistant there gets it used automatically, prompt and all.

    The shape returned is what both the web SDK and the REST API accept:
    ``{"assistantId": ...}`` or ``{"assistant": {...}}``.
    """
    if settings["assistant_id"]:
        return {"assistantId": settings["assistant_id"]}
    greeting_name = client_name or "there"
    amount_line = (
        f"The outstanding amount is {amount:.0f} rupees." if amount else "The outstanding amount is on file."
    )
    assistant: dict[str, Any] = {
        "name": "Recovery Agent",
        "firstMessage": f"Hello {greeting_name}, {DEFAULT_FIRST_MESSAGE}",
        "model": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{ASSISTANT_SYSTEM_PROMPT}\n"
                        f"Client name: {greeting_name}.\n"
                        f"{amount_line}\n"
                        f"Reason the balance is outstanding: {condition or 'unpaid balance'}."
                    ),
                }
            ],
        },
        "transcriber": {"provider": "deepgram", "model": "nova-2", "language": "en"},
        "endCallFunctionEnabled": True,
        "maxDurationSeconds": 180,
        # Vapi hangs up on prolonged silence. Kept a multiple of our own window so
        # a genuinely silent call ends on its own rather than running to the cap.
        "silenceTimeoutSeconds": int(SILENCE_WINDOW_SECONDS * 4),
    }
    if settings["voice_id"]:
        assistant["voice"] = {"provider": "11labs", "voiceId": settings["voice_id"]}
    return {"assistant": assistant}


# ---------------------------------------------------------------------------
# Web call — the primary flow
# ---------------------------------------------------------------------------


def start_web_call(
    case_id: str,
    *,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
    phone: str = "",
    case_key: str = "",
    mode: str | None = None,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
) -> dict[str, Any]:
    """Open a ``call_log`` row and return what the browser needs to dial.

    The row is written *before* the browser connects, so an attempt that fails at
    the microphone-permission stage is still a visible attempt rather than
    silently absent. Card 3 counts it immediately; Card 4 ignores it until it is
    closed.

    Returns ``{"call": row, "mode": ..., "web": {...} | None}``. The ``web``
    block holds the public key and the assistant definition — no private
    credential is ever placed in it.
    """
    settings = vapi_config()
    chosen = mode or resolve_mode(settings)
    if chosen != "web":
        # No row is opened for a call that cannot happen. An unconfigured
        # deployment gets an error it can display, not a simulated attempt.
        raise VapiConfigError("VAPI_PUBLIC_KEY is required for a browser web call")

    call = open_call(
        str(case_id),
        case_key=case_key,
        client_name=client_name,
        phone=str(phone or ""),
        provider="vapi",
        mode="web",
        path=voice_path,
    )
    record_call_audit(call, "voice_call_placed", "Browser web call started.", "call_placed", audit_path)

    return {
        "call": call,
        "mode": chosen,
        "web": {
            "public_key": settings["public_key"],
            # metadata is how the server-side end-of-call report finds its way
            # back to this row without depending on the browser reporting in.
            "metadata": {"call_log_id": call["id"], "case_id": str(case_id), "case_key": case_key or ""},
            "silence_window_seconds": SILENCE_WINDOW_SECONDS,
            **build_assistant(settings, client_name=client_name, amount=amount, condition=condition),
        },
    }


def complete_web_call(
    call_id: int,
    *,
    transcript: str = "",
    speech_detected: bool | None = None,
    seconds_to_first_speech: float | None = None,
    provider_call_id: str = "",
    ended_reason: str = "",
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Close a web or demo call once the browser reports it ended.

    Step 1 is the silence window and nothing else: speech captured within
    :data:`SILENCE_WINDOW_SECONDS` means answered, otherwise ``no_answer``. The
    browser may pass ``speech_detected`` and ``seconds_to_first_speech`` as
    observations, but the decision is made here so it cannot differ between the
    browser path and the webhook path.

    Step 2 runs only for an answered call, and it is the same 4-way classifier a
    webhook-closed call goes through.
    """
    call = get_call(int(call_id), voice_path)
    if call is None:
        raise LookupError(f"No call_log row with id {call_id}")
    if call.get("ended_at"):
        # The server-side end-of-call report already closed it. Not an error:
        # both paths are expected, and the first one to arrive is authoritative.
        return {"handled": False, "reason": "call already closed", "call": call, "duplicate": True}
    if provider_call_id and not call.get("provider_call_id"):
        attach_provider_call_id(call["id"], provider_call_id, voice_path)

    spoke = bool(str(transcript or "").strip()) if speech_detected is None else bool(speech_detected)
    in_window = seconds_to_first_speech is None or float(seconds_to_first_speech) <= SILENCE_WINDOW_SECONDS
    answered = spoke and in_window
    resolved = resolve_call_outcome(
        answered=answered,
        transcript=transcript,
        ended_reason=ended_reason or ("" if answered else "silence-timed-out"),
        caller=caller,
    )
    closed = close_call(
        call["id"],
        outcome=resolved["outcome"],
        answered=bool(resolved["answered"]),
        promise_date=resolved.get("promise_date"),
        transcript_summary=resolved.get("summary") or "",
        ended_reason=ended_reason or ("" if answered else "silence-timed-out"),
        path=voice_path,
    )
    record_call_audit(closed, "voice_call_completed", resolved.get("summary") or "", resolved["outcome"], audit_path)
    return {"handled": True, "call": closed, "classification": resolved}


# Kept under its original name so demo-only callers read naturally. Demo and web
# close through the same function because they close by the same rule.
complete_demo_call = complete_web_call


# ---------------------------------------------------------------------------
# Outbound telephony — optional second path, not used by the web flow
# ---------------------------------------------------------------------------


def _post_call(settings: dict[str, Any], body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    import requests

    response = requests.post(
        f"{VAPI_API_BASE}/call",
        headers={"Authorization": f"Bearer {settings['private_key']}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise VapiConfigError(f"Vapi rejected the call ({response.status_code}): {response.text[:300]}")
    try:
        return dict(response.json())
    except (ValueError, TypeError):
        return {}


def place_call(
    case_id: str,
    *,
    phone: str,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
    case_key: str = "",
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    poster: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dial a real phone number through Vapi. Requires the private key.

    Unused by the browser flow and safe to ignore; it exists so the same
    ``call_log`` row, the same audit action and the same two-step closing rule
    apply if outbound dialling is switched on later. A dial that Vapi refuses is
    closed as ``no_answer`` — an attempt that never connected is, factually, an
    attempt nobody answered.
    """
    settings = vapi_config()
    if not settings["phone_ready"]:
        raise VapiConfigError("VAPI_PRIVATE_KEY and VAPI_PHONE_NUMBER_ID are required for an outbound call")
    if not str(phone or "").strip():
        raise ValueError("A phone number is required for an outbound call")

    call = open_call(
        str(case_id),
        case_key=case_key,
        client_name=client_name,
        phone=str(phone),
        provider="vapi",
        mode="live",
        path=voice_path,
    )
    body: dict[str, Any] = {
        "phoneNumberId": settings["phone_number_id"],
        "customer": {"number": str(phone).strip(), "name": client_name or None},
        "metadata": {"call_log_id": call["id"], "case_id": str(case_id), "case_key": case_key or ""},
        **build_assistant(settings, client_name=client_name, amount=amount, condition=condition),
    }
    try:
        payload = (poster or _post_call)(settings, body)
    except Exception as exc:  # noqa: BLE001 - a failed dial is a recorded fact
        closed = close_call(
            call["id"],
            outcome="no_answer",
            answered=False,
            ended_reason="dial-failed",
            transcript_summary=f"The call could not be placed: {exc}",
            path=voice_path,
        )
        record_call_audit(closed, "voice_call_failed", str(exc), "call_failed", audit_path, errors=[str(exc)])
        raise

    provider_call_id = str(payload.get("id") or "")
    if provider_call_id:
        attach_provider_call_id(call["id"], provider_call_id, voice_path)
        call = get_call(call["id"], voice_path) or call
    record_call_audit(call, "voice_call_placed", "Outbound recovery call placed via Vapi.", "call_placed", audit_path)
    return {"call": call, "mode": "live", "provider_call_id": provider_call_id or None}


# ---------------------------------------------------------------------------
# Webhook boundary
# ---------------------------------------------------------------------------


def verify_webhook(body: bytes | str, headers: Any, secret: str | None = None) -> bool:
    """Authenticate an inbound Vapi webhook.

    Two accepted forms, both constant-time compared:

    * ``X-Vapi-Secret`` equal to ``VAPI_WEBHOOK_SECRET`` — what Vapi sends when a
      server-URL secret is configured, and the normal case.
    * ``X-Vapi-Signature`` as a hex HMAC-SHA256 of the raw body under the same
      secret — for deployments that put a signer in front.

    With no secret configured the endpoint refuses everything rather than
    accepting everything. An unauthenticated webhook that can close calls and
    append audit rows is a worse failure than a webhook that does not work.
    """
    expected = (secret if secret is not None else vapi_config()["webhook_secret"]) or ""
    if not expected:
        return False
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return False
    provided = str(getter(SECRET_HEADER) or "")
    if provided and hmac.compare_digest(provided, expected):
        return True
    signature = str(getter(SIGNATURE_HEADER) or "")
    if not signature:
        return False
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    digest = hmac.new(expected.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip().lower(), digest)


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    """Vapi nests its event under ``message``; older shapes are flat."""
    message = payload.get("message")
    return dict(message) if isinstance(message, dict) else dict(payload)


def _transcript(message: dict[str, Any]) -> str:
    """Pull the client's speech out of whichever field this event carries it in."""
    direct = str(message.get("transcript") or "").strip()
    if direct:
        return direct
    artifact = message.get("artifact")
    if isinstance(artifact, dict):
        text = str(artifact.get("transcript") or "").strip()
        if text:
            return text
        messages = artifact.get("messages")
        if isinstance(messages, list):
            lines = [
                str(item.get("message") or "")
                for item in messages
                if isinstance(item, dict) and str(item.get("role") or "").lower() in {"user", "customer", "human"}
            ]
            joined = " ".join(line for line in lines if line).strip()
            if joined:
                return joined
    return str(message.get("summary") or "").strip()


def _locate_call(message: dict[str, Any], voice_path: Path) -> dict[str, Any] | None:
    """Find our row from the event: metadata first, provider id as fallback."""
    call_obj = message.get("call") if isinstance(message.get("call"), dict) else {}
    metadata = call_obj.get("metadata") if isinstance(call_obj.get("metadata"), dict) else {}
    if not metadata and isinstance(message.get("metadata"), dict):
        metadata = message["metadata"]
    raw_id = metadata.get("call_log_id") if isinstance(metadata, dict) else None
    if raw_id not in (None, ""):
        try:
            located = get_call(int(raw_id), voice_path)
        except (TypeError, ValueError):
            located = None
        if located:
            return located
    provider_call_id = str(call_obj.get("id") or message.get("callId") or "")
    return find_call_by_provider_id(provider_call_id, voice_path) if provider_call_id else None


def normalize_end_of_call(
    payload: dict[str, Any],
    *,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    caller: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Close the matching ``call_log`` row from an ``end-of-call-report`` event.

    Runs the same two-step rule as the browser path: ``endedReason`` decides step
    1, and only an answered call reaches classification in step 2. Every other
    Vapi event type is ignored, and a redelivered report for an already-closed
    call is reported as a duplicate rather than raising.
    """
    message = _message(payload)
    event_type = str(message.get("type") or "").strip()
    if event_type and event_type not in {"end-of-call-report", "status-update", "hang"}:
        return {"handled": False, "reason": f"ignored event '{event_type}'"}
    if event_type == "status-update" and str(message.get("status") or "").lower() != "ended":
        return {"handled": False, "reason": "call still in progress"}

    call = _locate_call(message, voice_path)
    if call is None:
        return {"handled": False, "reason": "no matching call_log row"}
    if call.get("ended_at"):
        return {"handled": False, "reason": "call already closed", "call": call, "duplicate": True}

    ended_reason = str(message.get("endedReason") or message.get("endedReasonDetail") or "").strip()
    transcript = _transcript(message)
    answered = answered_from_ended_reason(ended_reason, transcript)
    resolved = resolve_call_outcome(answered=answered, transcript=transcript, ended_reason=ended_reason, caller=caller)

    closed = close_call(
        call["id"],
        outcome=resolved["outcome"],
        answered=bool(resolved["answered"]),
        promise_date=resolved.get("promise_date"),
        transcript_summary=resolved.get("summary") or "",
        ended_reason=ended_reason,
        path=voice_path,
    )
    record_call_audit(closed, "voice_call_completed", resolved.get("summary") or "", resolved["outcome"], audit_path)
    return {"handled": True, "call": closed, "classification": resolved}


def ingest_webhook(
    body: bytes | str,
    headers: Any,
    *,
    secret: str | None = None,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    caller: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Verify, parse, and process one Vapi delivery. Returns ``(body, status)``."""
    if not verify_webhook(body, headers, secret):
        return {"ok": False, "error": "invalid webhook signature"}, 401
    raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "malformed JSON"}, 400
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}, 400
    result = normalize_end_of_call(payload, voice_path=voice_path, audit_path=audit_path, caller=caller)
    # Ignored events and duplicates are acknowledged with 200 so the provider
    # stops retrying something we have deliberately chosen not to act on.
    return {"ok": True, **{key: value for key, value in result.items() if key != "call"}}, 200
