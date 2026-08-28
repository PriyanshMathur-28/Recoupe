"""Dashboard and scheduler regression tests."""
from __future__ import annotations

import csv
import threading
from pathlib import Path

from dashboard import app, calculate_metrics
from main import create_scheduler, process_event
from modules.payments import PaymentLinkProviderError
from modules.service_layer import RecoveryService


def test_dashboard_metrics_and_route(tmp_path, monkeypatch):
    audit = tmp_path / "audit.csv"
    rows = [
        {"timestamp": "2026-08-23T10:00:00+00:00", "client_id": "C1", "client_name": "Asha", "event_type": "no_show", "source": "test", "action": "charge_fee", "message": "sent", "payment_status": "paid", "status": "clean", "errors": "", "event_json": '{"appointment_value": 500}'},
        {"timestamp": "2026-08-24T10:00:00+00:00", "client_id": "C2", "client_name": "Ravi", "event_type": "failed_subscription", "source": "test", "action": "retry_payment", "message": "sent", "payment_status": "recovered", "status": "clean", "errors": "", "event_json": '{"subscription_amount": 750}'},
        {"timestamp": "2026-08-25T10:00:00+00:00", "client_id": "C3", "client_name": "Mira", "event_type": "no_show", "source": "test", "action": "escalate_human", "message": "", "payment_status": "not_applicable", "status": "flagged_error", "errors": "bad input", "event_json": '{}'},
    ]
    with audit.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    from dashboard import _read_audit
    metrics = calculate_metrics(_read_audit(audit))
    assert metrics["cases_processed"] == 3
    assert metrics["fees_sent"] == 1
    assert metrics["fees_paid"] == 1
    assert metrics["subscriptions_retried"] == 1
    assert metrics["subscriptions_recovered"] == 1
    assert metrics["revenue_recovered"] == 1250
    assert metrics["escalations"] == 1
    assert metrics["revenue_dots"]
    assert calculate_metrics(rows, [{"id": 1}, {"id": 2}])["escalations"] == 2

    monkeypatch.setattr("dashboard.AUDIT_PATH", audit)
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '<div id="root"></div>' in body
    assert "/static/clients/assets/" in body
    assert "Cases processed" not in body
    assert "C1" not in body and "C3" not in body


def test_client_projection_filters_system_rows_and_keeps_case_audit_trail(tmp_path):
    audit = tmp_path / "audit.csv"
    fields = ["timestamp", "client_id", "client_name", "event_type", "source", "action", "message", "payment_status", "outcome", "status", "errors", "event_json"]
    rows = [
        {"timestamp": "2026-08-23T10:00:00+00:00", "client_id": "SUB1", "client_name": "Leena", "event_type": "failed_subscription", "source": "test", "action": "retry_payment", "message": "", "payment_status": "link_created", "outcome": "action_completed", "status": "clean", "errors": "", "event_json": '{"client_name":"Leena","client_email":"leena@example.com","subscription_amount":1000,"attempt_count":1}'},
        {"timestamp": "2026-08-24T10:00:00+00:00", "client_id": "SUB1", "client_name": "Leena", "event_type": "failed_subscription", "source": "test", "action": "escalate_human", "message": "", "payment_status": "not_applicable", "outcome": "human_review", "status": "clean", "errors": "", "event_json": '{"client_name":"Leena","client_email":"leena@example.com","subscription_amount":1000,"attempt_count":3}'},
        {"timestamp": "2026-08-25T10:00:00+00:00", "client_id": "1", "client_name": "", "event_type": "owner_acknowledgement", "source": "dashboard", "action": "acknowledge_owner", "message": "", "payment_status": "not_applicable", "outcome": "system_action", "status": "clean", "errors": "", "event_json": "{}"},
    ]
    with audit.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    clients = RecoveryService(audit, tmp_path / "attempts.sqlite3", tmp_path / "waitlist.sqlite3").list_clients()
    assert len(clients) == 1
    assert clients[0]["name"] == "Leena"
    assert clients[0]["condition"] == "escalate_human"
    assert clients[0]["payment_status"] == "not_applicable"
    assert clients[0]["last_activity_at"] == "2026-08-24T10:00:00+00:00"
    assert clients[0]["invoice_number"].startswith("INV-20260824-")
    assert [event["action"] for event in clients[0]["audit_trail"]] == ["retry_payment", "escalate_human"]


def test_scheduler_has_60_second_job():
    scheduler = create_scheduler(include_calendar=False)
    scheduler.start()
    try:
        job = scheduler.get_job("risk-event-poll")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 60
    finally:
        scheduler.shutdown(wait=False)


def test_process_event_deduplicates(tmp_path):
    event = {"event_type": "friendly_reminder", "client_id": "C1", "client_name": "Asha", "validation_errors": [], "source": "test"}
    store = tmp_path / "state.sqlite3"
    first = process_event(event, store_path=store, audit_path=tmp_path / "audit.csv", attempts_path=tmp_path / "attempts.sqlite3")
    second = process_event(event, store_path=store, audit_path=tmp_path / "audit.csv", attempts_path=tmp_path / "attempts.sqlite3")
    assert first["action"] == "escalate_human"
    assert second["skipped"] is True


def test_process_event_keeps_sources_distinct(tmp_path):
    store = tmp_path / "state.sqlite3"
    attempts = tmp_path / "attempts.sqlite3"
    first = {"event_type": "friendly_reminder", "client_id": "C1", "client_name": "Asha", "validation_errors": [], "source": "no_show_cases.csv"}
    second = {**first, "source": "google_calendar"}
    result_one = process_event(first, store_path=store, audit_path=tmp_path / "audit.csv", attempts_path=attempts)
    result_two = process_event(second, store_path=store, audit_path=tmp_path / "audit.csv", attempts_path=attempts)
    assert result_one.get("skipped") is not True
    assert result_two.get("skipped") is not True


def test_process_event_claim_is_atomic_across_workers(tmp_path, monkeypatch):
    event = {"event_type": "friendly_reminder", "client_id": "RACE", "validation_errors": [], "source": "test"}
    store = tmp_path / "state.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_process(self, claimed_event, **kwargs):
        calls.append(claimed_event)
        entered.set()
        release.wait(timeout=2)
        return {"event": claimed_event, "action": "friendly_reminder"}

    monkeypatch.setattr("modules.service_layer.RecoveryService.process_event", fake_process)
    results = []

    def worker():
        results.append(process_event(event, store_path=store, audit_path=tmp_path / "audit.csv", attempts_path=tmp_path / "attempts.sqlite3"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    threads[0].start()
    assert entered.wait(timeout=2)
    threads[1].start()
    threads[1].join(timeout=2)
    release.set()
    threads[0].join(timeout=2)

    assert len(calls) == 1
    assert sum(result.get("skipped") is True for result in results) == 1


def test_send_email_reports_payment_provider_limit_without_http_500(monkeypatch):
    class FailingService:
        def send_client_email(self, client_id, resend=False):
            raise PaymentLinkProviderError("Razorpay Test Mode has reached its payment-link limit. No email was sent.")

    monkeypatch.setattr("dashboard._service", lambda: FailingService())
    response = app.test_client().post("/api/clients/NS008/send-email", json={})

    assert response.status_code == 503
    assert response.get_json() == {
        "code": "payment_link_unavailable",
        "error": "Razorpay Test Mode has reached its payment-link limit. No email was sent.",
    }


def test_dashboard_mutations_fail_closed_without_credentials(monkeypatch):
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().post("/dashboard/waitlist", data={"csrf_token": "anything"})
    assert response.status_code == 503


def test_clients_route_serves_compiled_react_console(monkeypatch):
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().get("/clients")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '<div id="root"></div>' in body
    assert "/static/clients/assets/" in body
    assert "Client Console" not in body


def test_dashboard_and_clients_use_same_frontend(monkeypatch):
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    client = app.test_client()
    dashboard_body = client.get("/dashboard").get_data(as_text=True)
    clients_body = client.get("/clients").get_data(as_text=True)
    assert dashboard_body == clients_body


def test_dashboard_template_exists():
    assert Path("templates/dashboard.html").exists()
    assert "Case outcomes" in Path("templates/dashboard.html").read_text(encoding="utf-8")
