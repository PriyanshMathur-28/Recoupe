"""Regression tests for grounded Revenue Autopsy AI analysis."""
from __future__ import annotations

import csv
import json

from dashboard import app
from modules.revenue_autopsy import analyze, build_context, deterministic_answer


def _clients():
    return [
        {
            "client_id": "SUB-A", "name": "Asha", "email": "asha@example.com",
            "condition": "retry_payment", "payment_status": "link_created", "outcome": "",
            "email_sent": False, "case": {"subscription_amount": 1200, "failure_reason": "card_expired", "attempt_count": 0},
            "audit_trail": [],
        },
        {
            "client_id": "SUB-B", "name": "Ravi", "email": "ravi@example.com",
            "condition": "retry_payment", "payment_status": "recovered", "outcome": "recovered",
            "email_sent": True, "case": {"subscription_amount": 800}, "audit_trail": [],
        },
    ]


def test_context_uses_canonical_csv_and_calculates_exposure(tmp_path):
    path = tmp_path / "recovery_cases.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_type", "client_id", "client_name", "subscription_amount", "failure_reason", "attempt_count"])
        writer.writeheader()
        writer.writerow({"case_type": "subscription", "client_id": "S1", "client_name": "Leena", "subscription_amount": "1000", "failure_reason": "card_declined", "attempt_count": "1"})
        writer.writerow({"case_type": "subscription", "client_id": "S2", "client_name": "Mira", "subscription_amount": "500", "failure_reason": "card_expired", "attempt_count": "0"})
    context = build_context(_clients(), data_dir=tmp_path)
    assert context["metrics"]["csv_record_count"] == 2
    assert context["metrics"]["value_at_risk"] == 1500
    assert context["metrics"]["recovered_value"] == 800
    assert context["metrics"]["failure_reasons"][0] == {"reason": "card_declined", "count": 1, "amount": 1000.0}
    assert {item["reason"] for item in context["metrics"]["failure_reasons"]} == {"card_declined", "card_expired"}


def test_fallback_returns_grounded_snapshot_without_interpreting_question(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    context = build_context(_clients(), data_dir=tmp_path)
    answer, cited = deterministic_answer("Which 2 customers should we recover first?", context, [])
    assert "Dashboard clients: 2" in answer
    assert "Resolved clients: 1" in answer
    assert "₹800 recovered" in answer
    assert "project .env file" in answer
    assert cited == []


def test_injected_analyst_can_cite_exact_dashboard_record(tmp_path):
    result = analyze(
        "Show me the priority customer",
        _clients(),
        db_path=tmp_path / "chat.sqlite3",
        llm=lambda question, context, history: "Prioritize Asha (`SUB-A`).",
    )
    assert result["mode"] == "ai"
    assert result["cited_client_ids"] == ["SUB-A"]


def test_analyze_persists_conversation_and_uses_injected_analyst(tmp_path):
    calls = []

    def analyst(question, context, history):
        calls.append((question, history, context["metrics"]["dashboard_client_count"]))
        return "Evidence-backed answer for SUB-A."

    first = analyze("What happened?", _clients(), db_path=tmp_path / "chat.sqlite3", llm=analyst)
    second = analyze("Follow up", _clients(), conversation_id=first["conversation_id"], db_path=tmp_path / "chat.sqlite3", llm=analyst)
    assert first["mode"] == "ai"
    assert second["conversation_id"] == first["conversation_id"]
    assert calls[1][1][-1]["content"] == "Evidence-backed answer for SUB-A."
    assert second["context"]["dashboard_client_count"] == 2


def test_chat_route_returns_grounded_fallback_when_providers_fail(monkeypatch, tmp_path):
    monkeypatch.setattr("modules.revenue_autopsy.CONVERSATION_DB", tmp_path / "chat.sqlite3")
    monkeypatch.setattr("modules.revenue_autopsy._call_grounded_llm", lambda *args: (_ for _ in ()).throw(RuntimeError("provider unavailable")))
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().post("/api/revenue-autopsy/chat", json={"message": "How much revenue is at risk?"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "grounded-fallback"
    assert "grounded data snapshot" in payload["answer"].lower()
    assert payload["context"]["csv_record_count"] > 0


def test_context_route_exposes_current_source_summary(monkeypatch):
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().get("/api/revenue-autopsy/context")
    assert response.status_code == 200
    payload = response.get_json()
    assert "recovery_cases.csv" in payload["sources"]
    assert payload["metrics"]["unresolved_records"] >= 0
