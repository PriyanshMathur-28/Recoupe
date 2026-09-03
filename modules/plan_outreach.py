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
    PLAN_CONFIRMED_ACTION,
    PLAN_INVITED_ACTION,
    PLAN_LINK_ACTION,
    PLAN_REQUESTED_ACTION,
    PLAN_DB_PATH,
    PlanError,
    attach_installment_link,
    confirm_plan,
    create_or_refresh_plan,
    get_plan,
    link_notes,
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
    return {"subject": f"Flexible Payment Plan Confirmed - pay {due_now}", "body": "\n".join(lines)}


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


def _case_for_plan(
    plan: dict[str, Any],
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
) -> dict[str, Any]:
    """The recovery case a plan settles, in the shape :func:`_plan_audit_event` reads.

    The live case is preferred so the audit row carries exactly the identity the
    dashboard groups by. When it cannot be found — the CSV was replaced, say —
    the plan's own copy of those facts stands in, because a payment that has
    already happened must still be recorded against its client.
    """
    from .voice_calls import _case_for_send

    try:
        case = _case_for_send(str(plan.get("case_id") or ""), audit_path, attempts_path)
    except Exception:  # noqa: BLE001 - a missing case must not stop an audit row
        case = None
    if case is not None:
        return case
    return {
        "case": {
            "client_id": str(plan.get("case_id") or ""),
            "client_name": str(plan.get("client_name") or ""),
            "client_email": str(plan.get("client_email") or ""),
            "event_type": str(plan.get("event_type") or ""),
            "amount": plan.get("original_amount"),
            "source": str(plan.get("origin") or "voice_recovery"),
        }
    }


def next_unpaid_installment(plan: dict[str, Any], index: Any = None) -> dict[str, Any] | None:
    """The installment a link should be minted for.

    Either the one explicitly asked for, or the earliest that is not yet paid.
    Only ever one: billing the whole schedule at once would be the full amount
    again, which is the thing the customer said they could not pay.
    """
    rows = list(plan.get("installments") or [])
    if index is not None:
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            return None
        return next((row for row in rows if int(row.get("index") or 0) == wanted), None)
    return next((row for row in rows if str(row.get("status") or "") != "paid"), None)


def send_installment_link(
    plan: dict[str, Any],
    *,
    case: dict[str, Any] | None = None,
    index: Any = None,
    audit_path: Path = AUDIT_PATH,
    plan_path: Path = PLAN_DB_PATH,
    attempts_path: Path | None = None,
    message_service: Any = None,
    payment_client: Any = None,
    actor: str = "flexible_plan_chatbot",
) -> dict[str, Any]:
    """Bill ONE installment of a confirmed plan: new link, email, audit row.

    A brand-new Razorpay link is minted through the existing
    :func:`modules.payments.create_payment_link` — the link that already failed
    is never reused, and no second Razorpay client is introduced. Its ``notes``
    come from :func:`modules.flexible_plans.link_notes`, which is what lets the
    existing webhook credit the payment back to the ORIGINAL recovery case.

    A link the installment already holds is emailed again rather than replaced,
    so a customer who reloads the page or a retried request cannot leave two
    live links for the same money.

    Never raises. A confirmed plan is a recorded commitment, and a provider
    outage or a bounced email must not undo it, so the failure is audited and
    returned the way :func:`send_plan_invite` returns its own.
    """
    result: dict[str, Any] = {"sent": False, "link_created": False, "reason": ""}
    installment = next_unpaid_installment(plan, index)
    if installment is None:
        result["blocked_by"] = "nothing_to_bill"
        result["reason"] = "This plan has no unpaid installment to bill."
        return result

    if case is None:
        case = _case_for_plan(plan, audit_path, attempts_path)
    number = int(installment.get("index") or 1)
    total = len(plan.get("installments") or [])
    result["installment"] = number
    result["amount"] = installment.get("amount")

    link_id = str(installment.get("link_id") or "")
    short_url = str(installment.get("link_url") or "")
    if not (link_id and short_url):
        from .payments import create_payment_link

        reference = str(plan.get("case_key") or plan.get("case_id") or "").strip()
        description = f"Payment plan {number} of {total}" + (f" - {reference}" if reference else "")
        try:
            response = create_payment_link(
                installment.get("amount"),
                str(plan.get("client_name") or "Customer"),
                description,
                str(plan.get("client_email") or ""),
                client=payment_client,
                notes=link_notes(plan, installment),
            )
            link_id, short_url = str(response["id"]), str(response["short_url"])
        except Exception as exc:  # noqa: BLE001 - a provider outage is a recorded fact
            result["error"] = str(exc)
            result["reason"] = f"The installment payment link could not be created: {exc}"
            log_event(
                _plan_audit_event(plan, case, "flexible_plan_link", installment_index=number),
                PLAN_LINK_ACTION,
                result["reason"],
                "not_applicable",
                audit_path,
                errors=[str(exc)],
                outcome="technical_error",
                actor=actor,
            )
            return result

        try:
            plan = attach_installment_link(plan["id"], number, link_id, short_url, path=plan_path)
        except (PlanError, LookupError) as exc:
            result["error"] = str(exc)
            result["reason"] = f"The installment link was created but could not be attached: {exc}"
            log_event(
                _plan_audit_event(plan, case, "flexible_plan_link", installment_index=number),
                PLAN_LINK_ACTION,
                result["reason"],
                "link_created",
                audit_path,
                errors=[str(exc)],
                outcome="technical_error",
                actor=actor,
            )
            return result
        installment = next_unpaid_installment(plan, number) or installment

    result["link_created"] = True
    result["link_id"] = link_id
    result["link_url"] = short_url
    result["plan_status"] = plan.get("status") or ""

    letter = confirmed_email(plan, installment, short_url)
    try:
        from .messenger import send_email

        send_email(plan["client_email"], letter["subject"], letter["body"], service=message_service)
    except Exception as exc:  # noqa: BLE001 - a failed send is a recorded fact, not a crash
        result["error"] = str(exc)
        result["reason"] = f"The payment link was created but could not be emailed: {exc}"
        log_event(
            _plan_audit_event(plan, case, "flexible_plan_link", installment_index=number, link_url=short_url),
            PLAN_LINK_ACTION,
            letter["subject"],
            "link_created",
            audit_path,
            errors=[str(exc)],
            outcome="technical_error",
            actor=actor,
        )
        return result

    result["sent"] = True
    result["reason"] = f"{_rupees(installment.get('amount'))} payment link emailed to the customer."
    log_event(
        _plan_audit_event(
            plan,
            case,
            "flexible_plan_link",
            installment_index=number,
            installment_amount=installment.get("amount"),
            link_id=link_id,
            link_url=short_url,
            plan_summary=plan.get("plan_summary") or "",
        ),
        PLAN_LINK_ACTION,
        letter["body"],
        "link_created",
        audit_path,
        outcome="plan_link_sent",
        actor=actor,
    )
    return result


def confirm_and_bill(
    plan_id: Any,
    installments: list[dict[str, Any]],
    *,
    case: dict[str, Any] | None = None,
    audit_path: Path = AUDIT_PATH,
    plan_path: Path = PLAN_DB_PATH,
    attempts_path: Path | None = None,
    message_service: Any = None,
    payment_client: Any = None,
    actor: str = "flexible_plan_chatbot",
) -> dict[str, Any]:
    """Freeze the customer's confirmed schedule, then bill its first installment.

    The one entry point the chatbot calls when the customer presses Confirm
    Plan. The schedule must already have been approved by
    :func:`modules.policy_engine.evaluate_plan_schedule`;
    :func:`modules.flexible_plans.confirm_plan` independently refuses anything
    that does not settle the whole debt, so a caller that skipped the gate still
    cannot grant a discount here.
    """
    result: dict[str, Any] = {"confirmed": False, "sent": False, "reason": ""}
    try:
        plan = confirm_plan(int(plan_id), installments, path=plan_path)
    except (PlanError, LookupError, TypeError, ValueError) as exc:
        result["blocked_by"] = "plan_refused"
        result["reason"] = str(exc)
        return result

    if case is None:
        case = _case_for_plan(plan, audit_path, attempts_path)
    log_event(
        _plan_audit_event(
            plan,
            case,
            "flexible_plan_confirmed",
            plan_summary=plan["plan_summary"],
            installments=plan["installments"],
        ),
        PLAN_CONFIRMED_ACTION,
        f"The customer confirmed a payment plan: {plan['plan_summary']}.",
        "not_applicable",
        audit_path,
        outcome="plan_confirmed",
        actor=actor,
    )

    billed = send_installment_link(
        plan,
        case=case,
        audit_path=audit_path,
        plan_path=plan_path,
        attempts_path=attempts_path,
        message_service=message_service,
        payment_client=payment_client,
        actor=actor,
    )
    result.update(billed)
    result["confirmed"] = True
    result["plan_id"] = plan["id"]
    result["plan_summary"] = plan["plan_summary"]
    result["installments"] = plan["installments"]
    return result


if __name__ == "__main__":  # pragma: no cover - module self-test
    import tempfile

    from .audit_log import read_events

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

        # --- confirmation, link and email, against fakes for both providers ---
        class _Gmail:
            """The narrow slice of the Gmail client messenger.send_email touches."""

            def __init__(self) -> None:
                self.sent: list[dict[str, Any]] = []

            def users(self):
                return self

            def messages(self):
                return self

            def send(self, userId: str, body: dict[str, Any]):  # noqa: N803 - the API's own name
                self._body = body
                return self

            def execute(self):
                self.sent.append(self._body)
                return {"id": f"msg-{len(self.sent)}"}

        class _Links:
            def __init__(self) -> None:
                self.requests: list[dict[str, Any]] = []

            def create(self, request: dict[str, Any]) -> dict[str, Any]:
                self.requests.append(request)
                return {"id": f"plink_{len(self.requests)}", "short_url": f"https://rzp.io/i/plan{len(self.requests)}"}

        class _Razorpay:
            def __init__(self) -> None:
                self.payment_link = _Links()

        class _BrokenRazorpay:
            class payment_link:  # noqa: N801 - mirrors the SDK's attribute name
                @staticmethod
                def create(request: dict[str, Any]) -> dict[str, Any]:
                    raise RuntimeError("razorpay is unreachable")

        audit = Path(tmp) / "audit.csv"
        plans = Path(tmp) / "plans.sqlite3"
        live_case = {
            "client_id": "C-9",
            "client_name": "Aditya Sharma",
            "client_email": "aditya@example.com",
            "amount": 10000.0,
            "case_key": "C-9|resend_payment_link",
            "case": {
                "client_id": "C-9",
                "client_name": "Aditya Sharma",
                "client_email": "aditya@example.com",
                "event_type": "payment_failed",
                "amount": 10000.0,
                "source": "voice_recovery",
            },
        }
        opened, _token = create_or_refresh_plan(live_case, path=plans)
        gmail, rzp = _Gmail(), _Razorpay()
        billed = confirm_and_bill(
            opened["id"],
            [{"amount": 3000.0, "due_date": "2026-09-01"}, {"amount": 7000.0, "due_date": "2026-09-04"}],
            case=live_case,
            audit_path=audit,
            plan_path=plans,
            message_service=gmail,
            payment_client=rzp,
        )
        check("confirming freezes the schedule", billed["confirmed"] and billed["plan_summary"].startswith("Rs 3,000"))
        check("exactly one link is minted", len(rzp.payment_link.requests) == 1)
        check("only the first installment is charged", rzp.payment_link.requests[0]["amount"] == 300000)
        check("the link carries the plan id in its notes", rzp.payment_link.requests[0]["notes"]["flexible_plan_id"] == str(opened["id"]))
        check("the notes keep an existing recovery action", rzp.payment_link.requests[0]["notes"]["recovery_action"] == "resend_payment_link")
        check("the confirmation email was delivered", billed["sent"] and len(gmail.sent) == 1)
        check("the plan is marked link_sent", billed["plan_status"] == "link_sent")

        actions = [row["action"] for row in read_events(audit)]
        check("the confirmation is audited", PLAN_CONFIRMED_ACTION in actions)
        check("the link send is audited", PLAN_LINK_ACTION in actions)

        stored = get_plan(opened["id"], path=plans)
        check("the link is bound to installment 1", stored["installments"][0]["link_url"] == "https://rzp.io/i/plan1")
        check("installment 2 is still pending", stored["installments"][1]["status"] == "pending")

        resent = send_installment_link(stored, case=live_case, audit_path=audit, plan_path=plans, message_service=gmail, payment_client=rzp)
        check("a repeat send reuses the same link", len(rzp.payment_link.requests) == 1 and resent["link_url"] == "https://rzp.io/i/plan1")

        broke = send_installment_link(
            stored,
            case=live_case,
            index=2,
            audit_path=audit,
            plan_path=plans,
            message_service=gmail,
            payment_client=_BrokenRazorpay(),
        )
        check("a provider failure is reported, not raised", broke["sent"] is False and "error" in broke)
        check("a failed mint sends no email", len(gmail.sent) == 2)

        refused = confirm_and_bill(
            opened["id"],
            [{"amount": 3000.0, "due_date": "2026-09-01"}],
            case=live_case,
            audit_path=audit,
            plan_path=plans,
            message_service=gmail,
            payment_client=rzp,
        )
        check("a plan that does not settle the debt is refused", refused["confirmed"] is False and refused["blocked_by"] == "plan_refused")

        print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
        if failures:
            raise SystemExit(1)
