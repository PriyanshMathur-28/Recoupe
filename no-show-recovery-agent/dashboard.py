"""Flask operations dashboard for recovery, owner review, and waitlist state."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, render_template_string, request, send_file, session, url_for

from modules.audit_log import AUDIT_PATH
from modules.attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH
from modules.razorpay_webhooks import ingest_webhook
from modules.revenue_autopsy import analyze as analyze_revenue, build_context as build_revenue_context
from modules.service_layer import RecoveryService
from modules.waitlist import DB_PATH as WAITLIST_DB_PATH

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(ROOT / "templates"))
app.config["SECRET_KEY"] = __import__("os").environ.get("FLASK_SECRET_KEY", "local-dashboard-change-me")
DASHBOARD_USER = __import__("os").environ.get("DASHBOARD_USER", "owner")
DASHBOARD_PASSWORD = __import__("os").environ.get("DASHBOARD_PASSWORD", "")


def _service() -> RecoveryService:
    """Resolve mutable module paths at call time for tests and deployments."""
    return RecoveryService(AUDIT_PATH, ATTEMPTS_DB_PATH, WAITLIST_DB_PATH)


def _read_audit(audit_path: Path | None = None) -> list[dict[str, Any]]:
    """Read the current audit path, resolving the module setting at call time."""
    path = audit_path or AUDIT_PATH
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))

def _amount(row: dict[str, Any]) -> float:
    try:
        event = json.loads(row.get("event_json", "{}"))
        value = event.get("fee_amount", event.get("appointment_value", event.get("subscription_amount", 0)))
        amount = float(value or 0)
        return amount if math.isfinite(amount) and amount > 0 else 0.0
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _event_amount(row: dict[str, Any]) -> float:
    """Best-effort numeric amount from a row's event payload."""
    try:
        event = json.loads(row.get("event_json") or "{}")
    except json.JSONDecodeError:
        event = {}
    for key in ("appointment_value", "subscription_amount", "fee_amount"):
        try:
            value = float(event.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


def _rate(part: int, whole: int) -> float:
    """Return a percentage rate, safely handling a zero denominator."""
    return round((part / whole * 100) if whole else 0.0, 1)


def _event_field(row: dict[str, Any], key: str) -> Any:
    """Pull a field from a row's nested event payload, returning None when absent."""
    try:
        event = json.loads(row.get("event_json") or "{}")
    except json.JSONDecodeError:
        event = {}
    return event.get(key)


def _spark_points(values: list[float], width: int = 116, height: int = 34) -> str:
    """Build an SVG polyline ``points`` string that fits ``values`` in a box."""
    if not values:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    n = len(values)
    step = width / max(n - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = index * step
        y = height - 3 - ((value - low) / span) * (height - 8)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _smooth(values: list[float], width: int = 520, height: int = 180, pad: int = 18) -> tuple[str, str, list[list[float]]]:
    """Return (line_path, area_path, points) for a smooth SVG area chart.

    The line is drawn with a Catmull-Rom-style smoothing pass so the chart
    reads as a hand-tuned product visual rather than a raw polyline.
    """
    if not values:
        return "", "", []
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    n = len(values)
    step = width / max(n - 1, 1)
    pts: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = index * step
        y = height - pad - ((value - low) / span) * (height - 2 * pad)
        pts.append((x, y))
    if n == 1:
        line = f"M{pts[0][0]:.1f},{pts[0][1]:.1f} L{pts[0][0]:.1f},{pts[0][1]:.1f}"
    else:
        parts = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
        for i in range(n - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            mx = (x0 + x1) / 2
            parts.append(f"C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}")
        line = " ".join(parts)
    baseline = height - 1
    area = f"{line} L{width:.0f},{baseline} L0,{baseline} Z"
    dots = [[round(x, 1), round(y, 1)] for x, y in pts]
    return line, area, dots


def calculate_metrics(rows: list[dict[str, Any]], review_flags: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Calculate dashboard counters from audit rows without mutating the log.

    When owner flags are supplied, they are the authoritative open-review set.
    The optional argument preserves the standalone metrics API used by tests and
    offline tooling.
    """
    total = len(rows)
    fees = [row for row in rows if row.get("action") == "charge_fee"]
    retries = [row for row in rows if row.get("action") == "retry_payment"]
    refills = [row for row in rows if row.get("action") == "offer_waitlist"]
    escalations = [row for row in rows if row.get("action") == "escalate_human"]
    paid = [row for row in rows if row.get("payment_status") in {"paid", "recovered"}]
    partial = [row for row in rows if row.get("payment_status") == "partially_paid"]
    open_review_count = len(review_flags) if review_flags is not None else len(escalations)

    no_shows = [row for row in rows if row.get("event_type") == "no_show"]
    cancellations = [row for row in rows if row.get("event_type") == "calendar_cancellation"]
    subscriptions = [row for row in rows if row.get("event_type") == "failed_subscription"]
    healthy = [row for row in rows if row.get("outcome") == "action_completed" or (not row.get("outcome") and row.get("status") == "clean")]
    flagged = [row for row in rows if row.get("status") == "flagged_error"]

    fee_paid = sum(1 for row in fees if row.get("payment_status") in {"paid", "recovered"})
    retry_paid = sum(1 for row in retries if row.get("payment_status") in {"paid", "recovered"})
    revenue_recovered = sum(_event_amount(row) for row in paid)

    # Failure-reason distribution for the memberships funnel.
    reason_counts: dict[str, int] = {}
    for row in subscriptions:
        reason = str(_event_field(row, "failure_reason") or "unknown").replace("_", " ").title()
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reasons = [{"label": reason, "count": count} for reason, count in reason_counts.items()]
    reasons.sort(key=lambda item: item["count"], reverse=True)

    # Daily activity + recovered revenue series for the KPI sparklines.
    by_day: dict[str, int] = {}
    revenue_by_day: dict[str, float] = {}
    for row in rows:
        day = (row.get("timestamp") or "")[:10]
        if not day:
            continue
        by_day[day] = by_day.get(day, 0) + 1
        if row.get("payment_status") in {"paid", "recovered"}:
            revenue_by_day[day] = revenue_by_day.get(day, 0.0) + _event_amount(row)
    recent_days = sorted(by_day)[-7:]
    activity_series = [by_day.get(day, 0) for day in recent_days]
    revenue_series = [round(revenue_by_day.get(day, 0.0)) for day in recent_days]

    # Case-mix donut segments (stroke geometry precomputed here to keep the template clean).
    donut_radius = 42.0
    circumference = 2 * math.pi * donut_radius
    mix_total = len(no_shows) + len(cancellations) + len(subscriptions) or 1
    mix_entries = (
        ("Missed appointments", len(no_shows), "#38bdf8"),
        ("Cancelled slots", len(cancellations), "#f59e0b"),
        ("Failed memberships", len(subscriptions), "#a78bfa"),
    )
    mix_segments: list[dict[str, Any]] = []
    mix_offset = 0.0
    for label, count, color in mix_entries:
        frac = count / mix_total
        dash = frac * circumference
        mix_segments.append(
            {
                "label": label,
                "count": count,
                "frac": round(frac * 100, 1),
                "color": color,
                "dash": round(dash, 2),
                "offset": round(-mix_offset, 2),
            }
        )
        mix_offset += dash

    revenue_line, revenue_area, revenue_dots = _smooth(revenue_series)

    return {
        # KPI counters (contract preserved for existing regression tests).
        "cases_processed": total,
        "fees_sent": len(fees),
        "fees_paid": fee_paid,
        "subscriptions_retried": len(retries),
        "subscriptions_recovered": retry_paid,
        "slots_refilled": len(refills),
        "revenue_recovered": revenue_recovered,
        "escalations": open_review_count,
        "partial_payments": len(partial),
        "success_actions": sum(row.get("outcome") == "action_completed" or (not row.get("outcome") and row.get("action") in {"charge_fee", "retry_payment", "offer_waitlist", "friendly_reminder"}) for row in rows),
        # Volume breakdowns.
        "total_no_shows": len(no_shows),
        "total_cancellations": len(cancellations),
        "total_subscriptions": len(subscriptions),
        "healthy": len(healthy),
        "flagged": len(flagged),
        # Efficiency rates.
        "no_show_rate": _rate(len(no_shows), total),
        "subscription_rate": _rate(len(subscriptions), total),
        "fee_collection_rate": _rate(fee_paid, len(fees)),
        "recovery_rate": _rate(retry_paid, len(retries)),
        "clean_rate": _rate(len(healthy), total),
        "refill_rate": _rate(len(refills), total),
        # Distributions.
        "reason_breakdown": reasons,
        # Visualization series.
        "activity_series": activity_series,
        "activity_points": _spark_points(activity_series),
        "revenue_series": revenue_series,
        "revenue_points": _spark_points(revenue_series),
        "revenue_line": revenue_line,
        "revenue_area": revenue_area,
        "revenue_dots": revenue_dots,
        "mix": {
            "total": mix_total,
            "circumference": round(circumference, 2),
            "segments": mix_segments,
        },
    }


@app.template_filter("inr")
def inr_filter(value: Any) -> str:
    """Format a numeric value as Indian Rupees without decimals."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"₹{number:,.0f}"


@app.template_filter("amount")
def amount_filter(row: dict[str, Any]) -> float:
    """Return the best-effort amount associated with an audit row."""
    return _event_amount(row)


@app.template_filter("event_field")
def event_field_filter(row: dict[str, Any], key: str) -> Any:
    """Return a nested event payload field for an audit row."""
    return _event_field(row, key)


@app.template_filter("reltime")
def reltime_filter(value: Any) -> str:
    """Trim an ISO timestamp to a compact 'HH:MM · DD Mon' label."""
    stamp = str(value or "")
    if len(stamp) < 19:
        return stamp or "—"
    time_part = stamp[11:16]
    try:
        from datetime import datetime

        date = datetime.fromisoformat(stamp[:19]).date()
        return f"{time_part} · {date.strftime('%d %b')}"
    except ValueError:
        return stamp[:16]


@app.get("/login")
def login():
    return render_template_string("""<!doctype html><title>Dashboard login</title><form method='post'><label>User <input name='username'></label><label>Password <input name='password' type='password'></label><button>Sign in</button></form>""")


@app.post("/login")
def login_submit():
    if not DASHBOARD_PASSWORD or request.form.get("username") != DASHBOARD_USER or request.form.get("password") != DASHBOARD_PASSWORD:
        return render_template_string("""<!doctype html><title>Dashboard login</title><p>Invalid dashboard credentials</p><form method='post'><input name='username'><input name='password' type='password'><button>Sign in</button></form>"""), 401
    session["dashboard_user"] = DASHBOARD_USER
    session["csrf_token"] = __import__("secrets").token_urlsafe(32)
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _require_mutation_access():
    """Require configured owner credentials and a session-bound CSRF token."""
    if not DASHBOARD_PASSWORD:
        return jsonify({"error": "Dashboard mutations are disabled until DASHBOARD_PASSWORD is configured"}), 503
    if session.get("dashboard_user") != DASHBOARD_USER:
        return redirect(url_for("login"))
    if not session.get("csrf_token") or request.form.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"error": "Invalid CSRF token"}), 403
    return None


@app.post("/webhooks/razorpay")
def razorpay_webhook():
    """Receive verified Razorpay callbacks at a deployable HTTP boundary."""
    import os

    try:
        result = ingest_webhook(
            request.get_data(),
            request.headers.get("X-Razorpay-Signature", ""),
            os.getenv("RAZORPAY_WEBHOOK_SECRET", ""),
            request.headers.get("X-Razorpay-Event-Id", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


@app.post("/dashboard/review/<int:flag_id>/resolve")
def resolve_review(flag_id: int):
    if (denied := _require_mutation_access()) is not None:
        return denied
    """Acknowledge one owner-review flag and persist the acknowledgement."""
    if not _service().acknowledge_owner_action(flag_id):
        return jsonify({"error": "Review flag not found or already resolved"}), 404
    return redirect(url_for("dashboard") + "#review")


@app.post("/dashboard/cases/retry")
def retry_case():
    if (denied := _require_mutation_access()) is not None:
        return denied
    """Record an owner-approved retry request for scheduler execution."""
    try:
        event = json.loads(request.form.get("event_json") or "{}")
        if not isinstance(event, dict):
            raise ValueError("event_json must contain an object")
        client_id = str(event.get("client_id") or "unknown")
        _service().retry_event({**event, "client_id": client_id, "source": "dashboard"}, live=True)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("dashboard") + "#cases")


@app.post("/dashboard/waitlist")
def create_waitlist_entry():
    if (denied := _require_mutation_access()) is not None:
        return denied
    """Add a validated client to the waitlist from the dashboard."""
    try:
        _service().add_waitlist_client({key: request.form.get(key, "") for key in ("client_id", "client_name", "client_email")})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("dashboard") + "#waitlist")


@app.post("/dashboard/waitlist/<int:entry_id>")
def edit_waitlist_entry(entry_id: int):
    if (denied := _require_mutation_access()) is not None:
        return denied
    """Update a waitlist row from the operations dashboard."""
    try:
        _service().update_waitlist_client(entry_id, {key: request.form.get(key, "") for key in ("client_id", "client_name", "client_email", "status")})
    except (LookupError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("dashboard") + "#waitlist")


@app.post("/dashboard/waitlist/slot")
def update_slot_status():
    if (denied := _require_mutation_access()) is not None:
        return denied
    """Update the waitlist slot lifecycle from the dashboard."""
    try:
        _service().set_slot_status(request.form.get("status", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("dashboard") + "#waitlist")


@app.get("/api/clients")
def clients_api():
    """Return current per-client cases and confirmed email status as JSON."""
    return jsonify(_service().list_clients())


@app.get("/api/revenue-autopsy/context")
def revenue_autopsy_context_api():
    """Return a compact description of the data currently grounding the analyst."""
    context = build_revenue_context(_service().list_clients())
    return jsonify({
        "generated_at": context["generated_at"],
        "sources": context["sources"],
        "csv_record_count": context["metrics"]["csv_record_count"],
        "dashboard_client_count": context["metrics"]["dashboard_client_count"],
        "metrics": context["metrics"],
    })


@app.post("/api/revenue-autopsy/chat")
def revenue_autopsy_chat_api():
    """Answer one grounded revenue question while preserving conversation context."""
    payload = request.get_json(silent=True) or {}
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        return jsonify({"error": "filters must be an object"}), 400
    try:
        result = analyze_revenue(
            payload.get("message", ""),
            _service().list_clients(),
            conversation_id=payload.get("conversation_id"),
            filters=filters,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/clients/<client_id>/send-email")
def send_client_email_api(client_id: str):
    """Deliver one current client case and persist the confirmed send."""
    payload = request.get_json(silent=True) or {}
    try:
        result = _service().send_client_email(client_id, resend=bool(payload.get("resend")))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/clients/send-bulk")
def send_bulk_clients_api():
    """Deliver selected current client cases sequentially and summarize outcomes."""
    payload = request.get_json(silent=True) or {}
    client_ids = payload.get("client_ids", [])
    if not isinstance(client_ids, list):
        return jsonify({"error": "client_ids must be a list"}), 400
    sent, failed = [], []
    service = _service()
    for client_id in client_ids:
        try:
            sent.append(service.send_client_email(str(client_id)))
        except Exception as exc:
            failed.append({"client_id": str(client_id), "error": str(exc)})
    return jsonify({"sent": len(sent), "failed": len(failed), "results": sent, "errors": failed})


def ensure_port_available(host: str, port: int) -> None:
    """Refuse to start when another process already serves ``host:port``.

    Windows permits a second bind of an address that is already listening
    (Werkzeug enables SO_REUSEADDR), and new connections then land on an
    arbitrary listener. A stale server therefore keeps answering with the code
    it was started with while a freshly launched one logs a healthy startup, so
    edits appear to have no effect. Fail loudly instead of sharing the port.
    """
    import socket

    probe_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((probe_host, port)) != 0:
            return
    raise SystemExit(
        f"Port {port} on {probe_host} is already serving. Stop the running dashboard first, "
        f"otherwise it keeps answering with stale code. PowerShell: "
        f"Get-NetTCPConnection -LocalPort {port} -State Listen | "
        f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"
    )


def _serve_client_console():
    """Serve the compiled React console used by the dashboard entry points."""
    if DASHBOARD_PASSWORD and not session.get("dashboard_user"):
        return redirect(url_for("login"))
    bundle_index = ROOT / "static" / "clients" / "index.html"
    if not bundle_index.exists():
        return jsonify({"error": "Client console is not built. Run npm run build in frontend."}), 503
    response = send_file(bundle_index)
    # The shell names hashed asset files, so only the shell itself must stay
    # uncached: a cached copy keeps loading a previous build's bundle forever.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/clients")
def clients_page():
    """Keep the previous client-console URL as a compatibility alias."""
    return _serve_client_console()


@app.get("/")
def home():
    """Send the default browser URL to the recovery dashboard."""
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
@app.get("/dashboard/")
def dashboard():
    """Serve the new frontend at the dashboard URL."""
    return _serve_client_console()


if __name__ == "__main__":
    ensure_port_available("127.0.0.1", 5000)
    app.run(host="127.0.0.1", port=5000, debug=False)
