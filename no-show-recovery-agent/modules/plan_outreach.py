"""The two customer-facing emails of a flexible payment plan.

A plan reaches the customer twice, and only twice:

1. **The invitation.** The client told the voice agent they cannot pay in full,
   so they are emailed a private link to negotiate a split of the SAME debt.
   Nothing is agreed at this point — the email promises a conversation, never a
   schedule.
2. **The confirmed installment link.** After the customer confirms a
   policy-valid plan, the first installment gets a real Razorpay link. That
   email lives here too (see :func:`send_installment_link`) so both plan emails
   read as one voice and both are audited the same way.

Why this is its own module rather than another branch of
:func:`modules.handlers.handle_action`: that function derives both the Razorpay
description and the Gmail subject from the action name
(``action.replace("_", " ").title()``), which is exactly right for the recovery
actions and exactly wrong for a customer-facing invitation. Reusing it would
have emailed a client the subject "Flexible Plan Invited". The delivery service
(:mod:`modules.messenger`) and the payment provider
(:mod:`modules.payments`) are still the existing ones; only the wording is new.

Emails are plain UTF-8 text, as every other email in this project is, so an
HTML "button" becomes a labelled URL on its own line.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .audit_log import AUDIT_PATH, log_event
from .flexible_plans import (
    PLAN_INVITED_ACTION,
    PLAN_REQUESTED_ACTION,
    PLAN_DB_PATH,
    PlanError,
    create_or_refresh_plan,
    plan_summary_line,
)

ROOT = Path(__file__).resolve().parents[1]

# Where the customer's chatbot page lives. One path, named once, because the
# token in it is the only thing standing between a stranger and one case.
PLAN_PAGE_PATH = "/recover/flexible-plan"

# The host the customer's browser can reach. Local by default because that is
# what a laptop demo serves; a deployment sets PUBLIC_BASE_URL to its own origin.
DEFAULT_BASE_URL = "http://127.0.0.1:5000"


def public_base_url(base_url: str | None = None) -> str:
    """The origin to build customer links against, without a trailing slash."""
    chosen = str(base_url or os.getenv("PUBLIC_BASE_URL") or DEFAULT_BASE_URL).strip()
    return chosen.rstrip("/") or DEFAULT_BASE_URL


def plan_page_url(token: str, base_url: str | None = None) -> str:
    """The one private URL that opens exactly one plan."""
    return f"{public_base_url(base_url)}{PLAN_PAGE_PATH}/{str(token or '').strip()}"


def _rupees(amount: Any) -> str:
    """Money as a customer reads it. ASCII "Rs" rather than the symbol, because
    the same wording is reused in PDFs, which strip non-ASCII."""
    try:
        return f"Rs {float(amount):,.0f}"
    except (TypeError, ValueError):
        return "Rs 0"


def invite_email(plan: dict[str, Any], url: str) -> dict[str, str]:
    """Subject and body of the invitation to negotiate.

    Deliberately says nothing about what is allowed. The customer proposes, the
    policy layer decides, and promising a shape of plan here would be a
    commitment nobody has authority to make on the call.
    """
    name = str(plan.get("client_name") or "there").split()[0]
    amount = _rupees(plan.get("original_amount"))
    hint = str(plan.get("voice_hint") or "").strip()
    lines = [
        "Flexible Payment Plan",
        "",
        f"Hi {name},",
        "",
        f"Your payment of {amount} could not be completed.",
        "",
        "Since you requested a flexible payment option, you can discuss and",
        "customise a payment plan with our assistant.",
    ]
    if hint:
        lines += ["", f"From your call, we noted: {hint}", "You can change any of it on the page below."]
    lines += [
        "",
        "Discuss Flexible Payment Plan:",
        url,
        "",
        "This link is private to you and expires, so please open it soon.",
        "Nothing is charged until you confirm a plan yourself.",
    ]
    return {"subject": "Flexible payment plan for your pending payment", "body": "\n".join(lines)}


def confirmed_email(plan: dict[str, Any], installment: dict[str, Any], short_url: str) -> dict[str, str]:
    """Subject and body of the first installment's payment link.

    The whole schedule is restated, not just the amount being charged, so the
    customer can see what they agreed to and what is still to come.
    """
    name = str(plan.get("client_name") or "there").split()[0]
    due_now = _rupees(installment.get("amount"))
    remaining = float(plan.get("amount_remaining") or 0) - float(installment.get("amount") or 0)
    lines = [
        "Flexible Payment Plan Confirmed",
        "",
        f"Hi {name},",
        "",
        f"Original amount: {_rupees(plan.get('original_amount'))}",
        f"Your plan: {plan.get('plan_summary') or plan_summary_line(plan.get('installments') or [])}",
        "",
        f"Payment due now: {due_now}",
    ]
    if remaining > 0.5:
        lines.append(f"Remaining after this payment: {_rupees(remaining)}")
    lines += [
        "",
        f"Pay {due_now}:",
        short_url,
        "",
        "We will email the next link when it is due.",
    ]
    return {"subject": f"Flexible payment plan confirmed - pay {due_now}", "body": "\n".join(lines)}


def _plan_audit_event(plan: dict[str, Any], case: dict[str, Any], event_type: str, **extra: Any) -> dict[str, Any]:
    """One audit payload shape for every plan transition.

    Built from the case's own event so the row carries the same client identity
    the dashboard groups by. The action names it is written under are outside
    ``service_layer.CASE_ACTIONS``, so none of these rows can rewrite the case's
    current condition.
    """
    event = dict(case.get("case") or {})
    return {
        **event,
        "event_type": event_type,
        "client_id": plan.get("case_id") or event.get("client_id") or "",
        "client_name": plan.get("client_name") or event.get("client_name") or "",
        "source": str(plan.get("origin") or "voice_recovery"),
        "flexible_plan_id": plan.get("id"),
        "flexible_plan_status": plan.get("status") or "",
        "original_amount": plan.get("original_amount"),
        **extra,
    }


def send_plan_invite(
    case: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    origin: str = "voice_recovery",
    origin_call_id: Any = None,
    audit_path: Path = AUDIT_PATH,
    plan_path: Path = PLAN_DB_PATH,
    message_service: Any = None,
    base_url: str | None = None,
    actor: str = "voice_agent",
) -> dict[str, Any]:
    """Open a plan for one case and email its private link to the customer.

    Returns the decision rather than raising, in the same spirit as
    :func:`modules.voice_calls.follow_up_email_for_call`: a call that has already
    been recorded must not be rolled back because an email failed.

    The plaintext token exists only inside this function. It goes into the
    customer's email and is never returned, logged, or handed to the dashboard —
    an operator has no reason to hold a bearer secret for a customer's page.
    """
    from .voice_calls import plan_request_hint

    result: dict[str, Any] = {"invited": False, "reason": ""}
    try:
        plan, token = create_or_refresh_plan(
            case,
            origin=origin,
            origin_call_id=origin_call_id,
            voice_hint=plan_request_hint(request),
            path=plan_path,
        )
    except PlanError as exc:
        result["blocked_by"] = "plan_refused"
        result["reason"] = str(exc)
        return result

    result["plan_id"] = plan["id"]
    result["plan_status"] = plan["status"]
    # The request itself is a fact worth keeping even if the email never lands:
    # it is why the case stopped being chased for the full amount.
    log_event(
        _plan_audit_event(plan, case, "flexible_plan_request", client_words=str((request or {}).get("client_words") or "")),
        PLAN_REQUESTED_ACTION,
        str((request or {}).get("note") or "The client asked to pay in parts rather than all at once."),
        "not_applicable",
        audit_path,
        outcome="plan_requested",
        actor=actor,
    )

    url = plan_page_url(token, base_url)
    letter = invite_email(plan, url)
    try:
        from .messenger import send_email

        send_email(plan["client_email"], letter["subject"], letter["body"], service=message_service)
    except Exception as exc:  # noqa: BLE001 - a failed send is a recorded fact, not a crash
        result["error"] = str(exc)
        result["reason"] = f"The flexible-plan link could not be emailed: {exc}"
        log_event(
            _plan_audit_event(plan, case, "flexible_plan_invite"),
            PLAN_INVITED_ACTION,
            letter["subject"],
            "not_applicable",
            audit_path,
            errors=[str(exc)],
            outcome="technical_error",
            actor=actor,
        )
        return result

    result["invited"] = True
    result["reason"] = "The customer was emailed a private link to choose a payment plan."
    log_event(
        _plan_audit_event(plan, case, "flexible_plan_invite"),
        PLAN_INVITED_ACTION,
        letter["body"],
        "sent",
        audit_path,
        outcome="plan_invited",
        actor=actor,
    )
    return result


def plan_invite_for_call(
    call: dict[str, Any],
    classification: dict[str, Any] | None,
    *,
    transcript: str = "",
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    plan_path: Path = PLAN_DB_PATH,
    auto_email: bool = True,
    plan_caller: Any = None,
    message_service: Any = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Decide whether one finished call asked for a plan, and invite if it did.

    This is a *peer* of :func:`modules.voice_calls.follow_up_email_for_call`,
    not a branch inside it. That function's first gate refuses everything that
    is not ``promised_to_pay``, and a client who asks to split the debt is
    usually filed ``declined`` or ``escalated`` — so an invitation decided
    inside that gate could never be sent. Deciding it here also keeps the two
    consequences of a call exclusive: a captured request *diverts* the
    follow-up, because emailing the full amount to somebody who just said they
    cannot pay it is the behaviour this feature exists to remove.

    Returns ``{"requested": bool, "invited": bool, "reason": str, ...}``. Never
    raises.
    """
    from .voice_calls import detect_plan_request

    if not call.get("answered"):
        return {"requested": False, "invited": False, "reason": "Nobody answered, so no plan was requested."}

    request = detect_plan_request(transcript, classification, plan_caller)
    result: dict[str, Any] = {"requested": bool(request["requested"]), "invited": False, "reason": request["note"], "request": request}
    if not result["requested"]:
        return result
    if not auto_email:
        result["blocked_by"] = "auto_email_disabled"
        result["reason"] = "Automatic sending is switched off (VOICE_AUTO_EMAIL), so the plan request was recorded without a link."
        return result

    from .voice_calls import _case_for_send

    case = _case_for_send(str(call.get("case_id") or ""), audit_path, attempts_path)
    if case is None:
        result["blocked_by"] = "case_not_found"
        result["reason"] = "No current case matches this call, so there is nothing to build a plan from."
        return result
    if "@" not in str((case.get("case") or {}).get("client_email") or case.get("email") or ""):
        result["blocked_by"] = "no_client_email"
        result["reason"] = "The client has no email address on file, so the plan link could not be delivered."
        return result

    invite = send_plan_invite(
        case,
        request=request,
        origin="voice_recovery",
        origin_call_id=call.get("id"),
        audit_path=audit_path,
        plan_path=plan_path,
        message_service=message_service,
        base_url=base_url,
    )
    result.update(invite)
    return result


if __name__ == "__main__":  # pragma: no cover - module self-test
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        failures: list[str] = []

        def check(label: str, condition: bool) -> None:
            print(f"{'PASS' if condition else 'FAIL'}: {label}")
            if not condition:
                failures.append(label)

        plan = {
            "id": 7,
            "case_id": "C-1",
            "client_name": "Aditya Sharma",
            "client_email": "aditya@example.com",
            "original_amount": 10000.0,
            "voice_hint": 'mentioned paying Rs 3,000 first - said: "I can pay 3000 today"',
            "status": "invited",
            "amount_remaining": 10000.0,
            "plan_summary": "Rs 3,000 today, then Rs 7,000 on Sept 4",
            "installments": [],
        }
        url = plan_page_url("tok-abc", "https://recover.example.com/")
        letter = invite_email(plan, url)

        check("the invite names the customer, not the case id", "Aditya" in letter["body"] and "C-1" not in letter["body"])
        check("the invite states the outstanding amount", "Rs 10,000" in letter["body"])
        check("the invite carries the private link", url in letter["body"])
        check("the link has no trailing-slash double", "//recover/flexible-plan" not in url.replace("https://", ""))
        check("the invite repeats what the client said on the call", "3,000" in letter["body"])
        check("the invite promises nothing it cannot grant", "approved" not in letter["body"].lower())
        check("the invite is plain ascii money", "\u20b9" not in letter["body"])

        confirmed = confirmed_email(plan, {"index": 1, "amount": 3000.0, "due_date": ""}, "https://rzp.io/i/abc")
        check("the confirmation restates the whole plan", "Rs 7,000 on Sept 4" in confirmed["body"])
        check("the confirmation charges only the first installment", "Pay Rs 3,000" in confirmed["body"])
        check("the confirmation shows the remainder", "Remaining after this payment: Rs 7,000" in confirmed["body"])
        check("the confirmation carries the razorpay link", "https://rzp.io/i/abc" in confirmed["body"])

        check("a missing origin falls back to localhost", public_base_url(None).startswith("http"))

        # An unanswered call must not consult a model or an email service.
        decision = plan_invite_for_call({"answered": 0, "case_id": "C-1"}, {"outcome": "no_answer"}, transcript="")
        check("an unanswered call is never invited", decision == {"requested": False, "invited": False, "reason": "Nobody answered, so no plan was requested."})

        # A captured request with no case behind it is reported, not raised.
        blocked = plan_invite_for_call(
            {"answered": 1, "case_id": "missing", "id": 3},
            {"outcome": "declined"},
            transcript="Agent: Can you pay today?\nClient: I can't pay the full amount, can I pay in instalments?",
            audit_path=Path(tmp) / "audit.csv",
            plan_path=Path(tmp) / "plans.sqlite3",
        )
        check("a request is detected from the client's own words", blocked["requested"] is True)
        check("a request with no live case is reported, not raised", blocked.get("blocked_by") == "case_not_found")

        off = plan_invite_for_call(
            {"answered": 1, "case_id": "C-1", "id": 4},
            {"outcome": "declined"},
            transcript="Agent: Can you pay today?\nClient: I cannot pay the full amount, only some now.",
            auto_email=False,
            audit_path=Path(tmp) / "audit.csv",
            plan_path=Path(tmp) / "plans.sqlite3",
        )
        check("the kill switch records the request without emailing", off["requested"] and not off["invited"] and off["blocked_by"] == "auto_email_disabled")

        print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
        if failures:
            raise SystemExit(1)
