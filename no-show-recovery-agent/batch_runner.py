"""Batch runner connecting detection, stopping rules, actions, and auditing."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from modules.attempt_tracker import DB_PATH, MAX_ATTEMPTS, flag_owner, get_attempt_count, increment_attempt, record_client_email_sent
from modules.audit_log import AUDIT_PATH, audit_db_path, log_event
from modules.decision_engine import decide
from modules.detector import get_all_risk_events
from modules.handlers import handle_action
from modules.message_generator import generate_message
from modules.waitlist import DB_PATH as WAITLIST_DB_PATH, notify_waitlist_person

PAYMENT_ACTIONS = {"charge_fee", "retry_payment"}


def _event_amount(event: dict[str, Any]) -> float:
    """Return the positive INR amount represented by an event."""
    for key in ("fee_amount", "appointment_value", "subscription_amount"):
        try:
            amount = float(event.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            return amount
    return 0.0


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return measured batch KPIs without confusing previews with recovered cash.

    A payment link or offline preview is an attempted recovery, not revenue.
    Revenue is counted only when an integration reports ``paid`` or
    ``recovered`` in the result/audit record.
    """
    payment_actions = [result for result in results if result.get("action") in PAYMENT_ACTIONS]
    links_created = [result for result in payment_actions if result.get("payment_status") in {"link_created", "paid", "recovered", "preview_created"}]
    recovered = [result for result in payment_actions if result.get("payment_status") in {"paid", "recovered"}]
    return {
        "cases_processed": len(results),
        "actions": {action: sum(result.get("action") == action for result in results) for action in ("charge_fee", "offer_waitlist", "friendly_reminder", "retry_payment", "escalate_human")},
        "payment_links_created": len(links_created),
        "revenue_at_risk": round(sum(_event_amount(result.get("event", {})) for result in payment_actions), 2),
        "revenue_recovered": round(sum(_event_amount(result.get("event", {})) for result in recovered), 2),
        "escalations": sum(result.get("action") == "escalate_human" for result in results),
        "flagged_errors": sum((result.get("audit") or {}).get("status") == "flagged_error" for result in results),
    }


def _offline_message(prompt: str) -> str:
    """Return a deterministic message for safe local batch verification."""
    return "Recovery action prepared for client review."


def run_event(event: dict[str, Any], attempts_path: Path = DB_PATH, audit_path: Path = AUDIT_PATH, payment_client: Any = None, llm_call: Callable[[str], str] | None = None, live: bool = False, message_service: Any = None, waitlist_path: Path = WAITLIST_DB_PATH) -> dict[str, Any]:
    """Process and audit one event, suppressing client contact on escalation."""
    client_id = str(event.get("client_id") or "")
    validation_errors = list(event.get("validation_errors") or [])
    proposed_action = "escalate_human" if validation_errors else decide(event)
    action = proposed_action
    attempt_count = None

    if proposed_action in PAYMENT_ACTIONS:
        source_attempts = event.get("attempt_count", 0) if event.get("event_type") == "failed_subscription" else 0
        baseline = int(source_attempts) if isinstance(source_attempts, int) and not isinstance(source_attempts, bool) else 0
        # The stopping rule is checked before execution. Technical failures do
        # not consume a payment attempt; successful action completion commits it.
        current_attempts = max(baseline, get_attempt_count(client_id, attempts_path, action_scope="payment"))
        if current_attempts + 1 >= MAX_ATTEMPTS:
            # A policy-limited third request counts even though contact is
            # suppressed; only downstream technical failures are non-consuming.
            attempt_count = increment_attempt(client_id, attempts_path, action_scope="payment", baseline=baseline)
            action = "escalate_human"

    if action == "escalate_human":
        reason = "; ".join(validation_errors) if validation_errors else (
            f"Stopping rule reached at attempt {attempt_count}" if attempt_count else "Decision policy requires human review"
        )
        owner_flag = flag_owner(client_id or "unknown", reason, attempts_path)
        row = log_event(event, action, None, "not_applicable", audit_path, errors=validation_errors, outcome="human_review")
        return {"event": event, "action": action, "attempt_count": attempt_count, "message": None, "client_notified": False, "owner_flag": owner_flag, "audit": row}

    payment_status = "not_applicable"
    try:
        if action == "offer_waitlist" and live:
            handled = notify_waitlist_person(event, db_path=waitlist_path, service=message_service, llm=llm_call)
            payment_status = "not_applicable"
            notified = True
        else:
            if not live and action in PAYMENT_ACTIONS:
                amount = _event_amount(event)
                if amount <= 0:
                    raise ValueError(f"{action} requires a positive amount")
                contact = event.get("client_phone") or event.get("client_email")
                if not str(contact or "").strip():
                    raise ValueError(f"{action} requires a client phone or email")
                safe_event = dict(event, short_url="https://example.invalid/payment-preview")
                handled = {**safe_event, "message": generate_message(safe_event, action, llm=llm_call or _offline_message)}
                payment_status = "preview_created"
            else:
                handled = handle_action(event, action, payment_client=payment_client, llm_call=llm_call or (_offline_message if not live else None), message_service=message_service, deliver=live)
                if action in PAYMENT_ACTIONS:
                    payment_status = "link_created" if live else "preview_created"
            notified = live
        if live and notified and event.get("client_email") and handled.get("message"):
            from modules.service_layer import case_key
            record_client_email_sent(client_id, action, handled["message"], attempts_path, case_key(event, action))
        if proposed_action in PAYMENT_ACTIONS:
            attempt_count = increment_attempt(client_id, attempts_path, action_scope="payment", baseline=baseline)
        row = log_event(event, action, handled.get("message"), payment_status, audit_path, outcome="action_completed")
        return {"event": event, "action": action, "attempt_count": attempt_count, "message": handled.get("message"), "client_notified": notified, "payment_status": payment_status, "audit": row}
    except Exception as exc:
        owner_flag = flag_owner(client_id or "unknown", f"Action failed: {exc}", attempts_path)
        row = log_event(event, "escalate_human", None, "failed", audit_path, errors=[str(exc)], outcome="technical_error")
        return {"event": event, "action": "escalate_human", "attempt_count": attempt_count, "message": None, "client_notified": False, "owner_flag": owner_flag, "error": str(exc), "audit": row}


def _batch_error_event(error: Exception) -> dict[str, Any]:
    return {
        "event_type": "batch_error",
        "client_id": "unknown",
        "client_name": "",
        "source": "batch_runner",
        "validation_errors": [f"Batch input failed: {error}"],
    }


def run_batch(include_calendar: bool = False, reset_audit: bool = False, reset_attempts: bool = False, attempts_path: Path = DB_PATH, audit_path: Path = AUDIT_PATH, event_loader: Callable[[], list[dict[str, Any]]] | None = None, live: bool = False, **action_kwargs: Any) -> list[dict[str, Any]]:
    """Run every detected event through the complete audited workflow.

    ``reset_attempts`` controls durable production safety state explicitly.
    Preview fixture replays default to an isolated clean baseline when they use
    the repository database, preventing old demonstrations from changing KPIs.
    """
    if reset_audit:
        for path in (audit_path, audit_db_path(audit_path)):
            if path.exists():
                path.unlink()
    clean_preview = not live and attempts_path == DB_PATH and event_loader is None
    if (reset_attempts or clean_preview) and attempts_path.exists():
        attempts_path.unlink()
    try:
        events = event_loader() if event_loader is not None else get_all_risk_events(include_calendar=include_calendar)
    except Exception as exc:
        event = _batch_error_event(exc)
        row = log_event(event, "escalate_human", None, "failed", audit_path, errors=event["validation_errors"])
        return [{"event": event, "action": "escalate_human", "message": None, "client_notified": False, "error": str(exc), "audit": row}]

    results = []
    for event in events:
        try:
            results.append(run_event(event, attempts_path, audit_path, live=live, **action_kwargs))
        except Exception as exc:
            error_event = dict(event) if isinstance(event, dict) else _batch_error_event(exc)
            errors = [f"Event processing failed: {exc}"]
            row = log_event(error_event, "escalate_human", None, "failed", audit_path, errors=errors)
            results.append({"event": error_event, "action": "escalate_human", "message": None, "client_notified": False, "error": str(exc), "audit": row})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the no-show recovery batch in explicit stages")
    parser.add_argument("--stage", choices=("detect", "decide", "preview", "live"), default="preview", help="detect only, decide only, offline preview, or live integrations")
    parser.add_argument("--include-calendar", action="store_true")
    parser.add_argument("--append", action="store_true", help="Append instead of replacing the audit CSV")
    parser.add_argument(
        "--reset-attempts",
        action="store_true",
        help="Clear durable attempt counters before processing (use for a deliberate clean replay)",
    )
    args = parser.parse_args()
    if args.stage == "detect":
        events = get_all_risk_events(include_calendar=args.include_calendar)
        print(f"Detected {len(events)} risk events")
        raise SystemExit(0)

    live = args.stage == "live"
    if args.stage == "decide":
        events = get_all_risk_events(include_calendar=args.include_calendar)
        for event in events:
            action = "escalate_human" if event.get("validation_errors") else decide(event)
            print(f"{event.get('client_id', 'unknown')}: {action}")
        raise SystemExit(0)

    results = run_batch(
        include_calendar=args.include_calendar,
        reset_audit=not args.append,
        reset_attempts=args.reset_attempts,
        live=live,
    )
    summary = summarize_results(results)
    print(
        f"Processed {summary['cases_processed']} events; "
        f"links/previews {summary['payment_links_created']}; "
        f"revenue at risk INR {summary['revenue_at_risk']:,.2f}; "
        f"revenue recovered INR {summary['revenue_recovered']:,.2f}; "
        f"escalations {summary['escalations']}; flagged errors {summary['flagged_errors']}"
    )
