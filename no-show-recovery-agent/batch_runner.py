"""Batch runner connecting detection, stopping rules, actions, and auditing."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from modules.attempt_tracker import DB_PATH, MAX_ATTEMPTS, flag_owner, get_attempt_count, increment_attempt, record_client_email_sent
from modules.audit_log import AUDIT_PATH, audit_db_path, log_event
from modules.detector import get_all_risk_events
from modules.diagnosis import diagnose
from modules.handlers import handle_action
from modules.policy_engine import DECISIONS_DB_PATH, evaluate, release_key
from modules.revenue_event import from_detector_event
from modules.message_generator import generate_message
from modules.waitlist import DB_PATH as WAITLIST_DB_PATH, notify_waitlist_person

PAYMENT_ACTIONS = {"charge_fee", "retry_payment", "resend_payment_link"}


def _event_amount(event: dict[str, Any]) -> float:
    """Return the positive INR amount represented by an event."""
    for key in ("amount", "fee_amount", "appointment_value", "subscription_amount"):
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


def run_event(event: dict[str, Any], attempts_path: Path = DB_PATH, audit_path: Path = AUDIT_PATH, payment_client: Any = None, llm_call: Callable[[str], str] | None = None, live: bool = False, message_service: Any = None, waitlist_path: Path = WAITLIST_DB_PATH, decisions_path: Path = DECISIONS_DB_PATH, now: Any = None) -> dict[str, Any]:
    """Run detect → typed diagnosis → policy → bounded executor → audit."""
    # Custom attempt stores (tests, tenants, isolated workers) receive a sibling
    # policy store so idempotency state cannot leak across environments.
    if decisions_path == DECISIONS_DB_PATH and attempts_path != DB_PATH:
        decisions_path = attempts_path.with_name(f"{attempts_path.stem}_policy.sqlite3")
    canonical = event if "amount" in event and "detected_at" in event else from_detector_event(event, now=now)
    client_id = str(canonical.get("client_id") or "unknown")
    baseline = int(canonical.get("attempt_count") or 0)
    tracked = get_attempt_count(client_id, attempts_path, action_scope="payment") if client_id != "unknown" else 0
    # Stop before asking the model for another proposal once the bounded
    # recovery budget has been consumed.
    if max(baseline, tracked) + 1 >= MAX_ATTEMPTS:
        proposal = {"root_cause": "attempt_limit", "recommended_intervention": "retry_payment", "confidence": 1.0, "reasoning": "Attempt budget exhausted before diagnosis.", "channel": "none", "urgency": "high", "source": "stopping_rule"}
    else:
        proposal = diagnose(canonical, llm=llm_call, use_llm=bool(live and llm_call))
    # Legacy fixture/backfill events have no provider event identity. They keep
    # their historical replay semantics; canonical webhook events enforce the
    # strict per-cycle idempotency reservation.
    enforce_idempotency = bool(event.get("event_id") or event.get("webhook_event_id"))
    verdict = evaluate(canonical, proposal, attempts_path=attempts_path, decisions_path=decisions_path, now=now, enforce_idempotency=enforce_idempotency)
    action = verdict.action

    if verdict.deferred:
        row = log_event(canonical, action, None, "not_applicable", audit_path, outcome="deferred", verdict=verdict, diagnosis=proposal, actor="policy_engine")
        return {"event": canonical, "proposal": proposal, "verdict": verdict.to_dict(), "action": action, "attempt_count": verdict.attempt_number - 1, "message": None, "client_notified": False, "next_attempt_at": verdict.next_attempt_at, "audit": row}

    if verdict.escalated:
        # The cap represents a consumed recovery opportunity even when the
        # stopping rule prevents a third outbound message. Persist that final
        # attempted rung so the durable counter and audit projection agree.
        attempt_count = verdict.attempt_number - 1
        if verdict.reason_code == "attempt_limit" and client_id != "unknown":
            attempt_count = increment_attempt(
                client_id,
                attempts_path,
                action_scope="payment",
                baseline=attempt_count,
            )
        owner_flag = flag_owner(client_id, verdict.reason, attempts_path)
        row = log_event(canonical, "escalate_human", None, "not_applicable", audit_path, outcome="human_review", verdict=verdict, diagnosis=proposal, actor="policy_engine")
        result = {"event": canonical, "proposal": proposal, "verdict": verdict.to_dict(), "action": "escalate_human", "attempt_count": attempt_count, "message": None, "client_notified": False, "owner_flag": owner_flag, "audit": row}
        if verdict.reason_code == "validation_error":
            result["error"] = verdict.reason
        return result

    payment_status = "not_applicable"
    try:
        if action == "offer_waitlist" and live:
            handled = notify_waitlist_person(canonical, db_path=waitlist_path, service=message_service, llm=llm_call)
            notified = True
        elif not live and action in PAYMENT_ACTIONS:
            if _event_amount(canonical) <= 0:
                raise ValueError(f"{action} requires a positive amount")
            safe_event = dict(canonical, short_url="https://example.invalid/payment-preview")
            handled = {**safe_event, "message": generate_message(safe_event, action, llm=_offline_message)}
            payment_status = "preview_created"
            notified = False
        else:
            handled = handle_action(canonical, action, payment_client=payment_client, llm_call=llm_call or (_offline_message if not live else None), message_service=message_service, deliver=live)
            payment_status = "link_created" if action in PAYMENT_ACTIONS else "not_applicable"
            notified = live
        if live and notified and canonical.get("client_email") and handled.get("message"):
            from modules.service_layer import case_key
            record_client_email_sent(client_id, action, handled["message"], attempts_path, case_key(canonical, action))
        attempt_count = increment_attempt(client_id, attempts_path, action_scope="payment", baseline=int(canonical.get("attempt_count") or 0)) if action in PAYMENT_ACTIONS else None
        executed_event = {**canonical, **{key: handled[key] for key in ("payment_link_id", "short_url", "invoice_number", "invoice_status", "invoice_due_date", "invoice_amount", "invoice_filename") if key in handled}}
        row = log_event(executed_event, action, handled.get("message"), payment_status, audit_path, outcome="action_completed", verdict=verdict, diagnosis=proposal, actor="bounded_executor")
        return {"event": executed_event, "proposal": proposal, "verdict": verdict.to_dict(), "action": action, "attempt_count": attempt_count, "message": handled.get("message"), "client_notified": notified, "payment_status": payment_status, "audit": row}
    except Exception as exc:
        # Provider failures fail closed and release an unexecuted reservation so
        # retry-with-backoff can safely process the case later.
        release_key(verdict.idempotency_key, decisions_path)
        owner_flag = flag_owner(client_id, f"Action failed: {exc}", attempts_path)
        row = log_event(canonical, "escalate_human", None, "failed", audit_path, errors=[str(exc)], outcome="technical_error", verdict=verdict, diagnosis=proposal, actor="bounded_executor")
        return {"event": canonical, "proposal": proposal, "verdict": verdict.to_dict(), "action": "escalate_human", "attempt_count": verdict.attempt_number - 1, "message": None, "client_notified": False, "owner_flag": owner_flag, "error": str(exc), "audit": row}


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
                try:
                    path.unlink()
                except PermissionError:
                    if path.suffix == ".sqlite3":
                        import sqlite3
                        with sqlite3.connect(path) as conn:
                            try:
                                conn.execute("DELETE FROM audit_log")
                            except sqlite3.OperationalError:
                                pass
                    elif path.suffix == ".csv":
                        try:
                            with open(path, "w", encoding="utf-8") as f:
                                f.truncate(0)
                        except OSError:
                            pass
    clean_preview = not live and attempts_path == DB_PATH and event_loader is None
    if (reset_attempts or clean_preview) and attempts_path.exists():
        try:
            attempts_path.unlink()
        except PermissionError:
            import sqlite3
            with sqlite3.connect(attempts_path) as conn:
                try:
                    conn.execute("DELETE FROM client_attempts")
                    conn.execute("DELETE FROM escalation_flags")
                    conn.execute("DELETE FROM client_email_status")
                except sqlite3.OperationalError:
                    pass
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
            canonical = from_detector_event(event)
            proposal = diagnose(canonical)
            verdict = evaluate(canonical, proposal, enforce_idempotency=False)
            print(f"{event.get('client_id', 'unknown')}: AI proposed {proposal['recommended_intervention']} → policy {verdict.decision}/{verdict.action}")
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
