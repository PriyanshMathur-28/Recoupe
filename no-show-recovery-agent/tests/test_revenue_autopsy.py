"""Regression tests for grounded Revenue Autopsy AI analysis."""
from __future__ import annotations

import csv
import json

import pytest

from dashboard import app
from modules.revenue_autopsy import (
    GEMINI_ANALYST_FALLBACK,
    PROVIDER_PROMPT_CHARS,
    SYSTEM_PROMPT,
    _call_grounded_llm,
    _redact,
    _serialize,
    analyze,
    build_context,
    deterministic_answer,
    fit_context,
)


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


# ═══════════════════════════════════════════════════════════════════════════
# The evidence packet has to fit the provider it is sent to
#
# The analyst sends the entire book of records in one prompt. Groq's on-demand
# tier caps tokens-per-minute far below any model's context window and refuses
# a full-size packet outright with HTTP 413, so every real question was failing
# there and being downgraded to a data snapshot. These tests exercise the real
# provider chain over a faked transport — the previous suite injected a fake
# analyst everywhere, so neither the payload size nor the model ids were ever
# under test.
# ═══════════════════════════════════════════════════════════════════════════
def _many_clients(count: int = 60):
    """Enough records, with enough audit history, to overflow a small ceiling."""
    return [
        {
            "client_id": f"C-{index:03d}", "name": f"Client {index}", "email": f"c{index}@example.com",
            "condition": "retry_payment", "payment_status": "link_created", "outcome": "",
            "email_sent": False, "case": {"subscription_amount": 1000 + index, "failure_reason": "card_declined"},
            "audit_trail": [{"action": "email_sent", "timestamp": f"2026-01-{entry:02d}T00:00:00Z"} for entry in range(1, 13)],
        }
        for index in range(count)
    ]


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _groq_ok(answer: str = "Groq answered."):
    return lambda call: _Response(payload={"choices": [{"message": {"content": answer}}]})


def _gemini_ok(answer: str = "Gemini answered."):
    return lambda call: _Response(payload={"candidates": [{"content": {"parts": [{"text": answer}]}}]})


class _Transport:
    """Stands in for both HTTP endpoints and records exactly what each was sent."""

    def __init__(self, groq=None, gemini=None):
        self._groq = groq or _groq_ok()
        self._gemini = gemini or _gemini_ok()
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        payload = kwargs.get("json") or {}
        if "groq.com" in url:
            name = "Groq"
            prompt = "".join(str(item.get("content") or "") for item in payload.get("messages", []))
        else:
            name = "Gemini"
            prompt = payload["contents"][0]["parts"][0]["text"]
        call = {"provider": name, "url": url, "prompt": prompt}
        self.calls.append(call)
        return (self._groq if name == "Groq" else self._gemini)(call)

    @property
    def order(self) -> list[str]:
        return [call["provider"] for call in self.calls]


@pytest.fixture
def transport(monkeypatch):
    """Both providers configured, no real network, no .env bleeding in."""
    monkeypatch.setattr("modules.revenue_autopsy.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    for name in ("GROQ_ANALYST_PROMPT_CHARS", "GEMINI_ANALYST_PROMPT_CHARS", "GEMINI_ANALYST_MODEL", "GROQ_ANALYST_MODEL"):
        monkeypatch.delenv(name, raising=False)

    def install(**kwargs) -> _Transport:
        fake = _Transport(**kwargs)
        monkeypatch.setattr("modules.revenue_autopsy.requests.post", fake.post)
        return fake

    return install


def test_a_packet_that_fits_is_sent_whole_and_says_so(tmp_path):
    context = build_context(_clients(), data_dir=tmp_path)
    packet, evidence = fit_context(context, 400000)
    assert packet is context
    assert len(evidence) <= 400000
    assert packet["evidence_scope"]["complete"] is True
    assert packet["evidence_scope"]["dashboard_records_included"] == 2


def test_an_oversized_packet_sheds_records_but_never_the_metrics(tmp_path):
    """Metrics are the authoritative totals and cost almost nothing, so they
    survive trimming intact — a trimmed packet must never understate the book."""
    context = build_context(_many_clients(), data_dir=tmp_path)
    packet, evidence = fit_context(context, 20000)
    assert len(evidence) <= 20000
    assert packet["metrics"] == context["metrics"]
    assert packet["metrics"]["dashboard_client_count"] == 60
    scope = packet["evidence_scope"]
    assert scope["complete"] is False
    assert scope["dashboard_records_total"] == 60
    assert 0 < scope["dashboard_records_included"] < 60


def test_trimming_keeps_the_largest_exposures_and_the_newest_audit_entries(tmp_path):
    context = build_context(_many_clients(), data_dir=tmp_path)
    packet, _ = fit_context(context, 20000)
    rows = packet["dashboard_records"]
    assert rows[0]["client_id"] == "C-059"
    assert [row["amount"] for row in rows] == sorted((row["amount"] for row in rows), reverse=True)
    assert all(len(row["audit_trail"]) <= 3 for row in rows)
    assert rows[0]["audit_trail"][-1]["timestamp"] == "2026-01-12T00:00:00Z"


def test_a_trimmed_packet_admits_what_it_left_out(tmp_path):
    """Silence here would let the analyst report a subset as the whole book."""
    context = build_context(_many_clients(), data_dir=tmp_path)
    packet, evidence = fit_context(context, 20000)
    note = packet["evidence_scope"]["note"]
    assert "trimmed" in note
    assert "authoritative" in note
    assert "evidence_scope" in evidence
    assert "evidence_scope" in SYSTEM_PROMPT


def test_a_provider_is_never_sent_a_request_it_cannot_accept(tmp_path, transport, monkeypatch):
    """The Groq 413 in production: refuse the call instead of making it."""
    monkeypatch.setenv("GROQ_ANALYST_PROMPT_CHARS", "500")
    fake = transport()
    answer = _call_grounded_llm("What is at risk?", build_context(_clients(), data_dir=tmp_path), [])
    assert answer == "Gemini answered."
    assert fake.order == ["Gemini"]


def test_the_rate_limited_tier_stands_aside_when_it_would_have_to_trim(tmp_path, transport):
    """A provider that can carry the whole book outranks one that cannot, so no
    answer is computed from a truncated book while full capacity sits idle."""
    fake = transport()
    answer = _call_grounded_llm("Rank every client", build_context(_many_clients(), data_dir=tmp_path), [])
    assert answer == "Gemini answered."
    assert fake.order == ["Gemini"]


def test_the_fast_tier_stays_primary_when_the_whole_book_fits(tmp_path, transport):
    fake = transport()
    answer = _call_grounded_llm("What is at risk?", build_context(_clients(), data_dir=tmp_path), [])
    assert answer == "Groq answered."
    assert fake.order == ["Groq"]


def test_a_fallback_provider_is_still_kept_inside_its_own_ceiling(tmp_path, transport):
    """Groq answering after Gemini died must get a packet Groq can actually take."""
    fake = transport(gemini=lambda call: _Response(status_code=503, text="upstream unavailable"))
    answer = _call_grounded_llm("Rank every client", build_context(_many_clients(), data_dir=tmp_path), [])
    assert answer == "Groq answered."
    assert fake.order == ["Gemini", "Groq"]
    groq_prompt = next(call["prompt"] for call in fake.calls if call["provider"] == "Groq")
    assert len(groq_prompt) <= PROVIDER_PROMPT_CHARS["Groq"]
    assert "trimmed" in groq_prompt


def test_a_model_the_key_cannot_see_falls_back_to_the_published_alias(tmp_path, transport):
    def gemini(call):
        if GEMINI_ANALYST_FALLBACK in call["url"]:
            return _Response(payload={"candidates": [{"content": {"parts": [{"text": "Alias answered."}]}}]})
        return _Response(status_code=404, text='{"error":{"message":"models/x is not found"}}')

    fake = transport(groq=lambda call: _Response(status_code=413, text="rate_limit_exceeded"), gemini=gemini)
    answer = _call_grounded_llm("What is at risk?", build_context(_clients(), data_dir=tmp_path), [])
    assert answer == "Alias answered."
    assert fake.order == ["Groq", "Gemini", "Gemini"]
    assert GEMINI_ANALYST_FALLBACK in fake.calls[-1]["url"]


def test_a_credential_never_leaks_into_the_failure_text(tmp_path, transport):
    """The Gemini endpoint carries the API key in its query string, so an
    unredacted provider error would publish a live credential to the dashboard."""
    fake = transport(
        groq=lambda call: _Response(status_code=401, text='{"error":"bad api_key=test-groq-key"}'),
        gemini=lambda call: _Response(status_code=400, text='{"error":"bad key=test-gemini-key"}'),
    )
    with pytest.raises(RuntimeError) as caught:
        _call_grounded_llm("What is at risk?", build_context(_clients(), data_dir=tmp_path), [])
    message = str(caught.value)
    assert "test-groq-key" not in message
    assert "test-gemini-key" not in message
    assert "REDACTED" in message
    assert fake.order == ["Groq", "Gemini"]


def test_redaction_keeps_the_diagnosis_and_drops_the_secret():
    cleaned = _redact('HTTP 400 {"error":"invalid key=AQ.super-secret-value"}')
    assert "AQ.super-secret-value" not in cleaned
    assert "key=REDACTED" in cleaned
    assert "HTTP 400" in cleaned


def test_the_operator_is_told_why_the_answer_was_downgraded(tmp_path):
    """A silent downgrade is what made this look like 'the AI is not working':
    the snapshot came back with no hint that a provider had refused the call."""
    def refuse(question, context, history):
        raise RuntimeError("Groq: HTTP 413 rate_limit_exceeded")

    result = analyze("What is at risk?", _clients(), db_path=tmp_path / "chat.sqlite3", llm=refuse)
    assert result["mode"] == "grounded-fallback"
    assert "rate_limit_exceeded" in result["detail"]
    assert "Reason the AI analyst could not answer" in result["answer"]
    assert "rate_limit_exceeded" in result["answer"]


def test_a_successful_answer_carries_no_failure_detail(tmp_path):
    result = analyze("What is at risk?", _clients(), db_path=tmp_path / "chat.sqlite3", llm=lambda *args: "All clear.")
    assert result["mode"] == "ai"
    assert result["detail"] == ""


def test_the_chat_route_publishes_the_provider_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("modules.revenue_autopsy.CONVERSATION_DB", tmp_path / "chat.sqlite3")
    monkeypatch.setattr("modules.revenue_autopsy._call_grounded_llm", lambda *args: (_ for _ in ()).throw(RuntimeError("Gemini: HTTP 404 model not available")))
    monkeypatch.setattr("dashboard.DASHBOARD_PASSWORD", "")
    response = app.test_client().post("/api/revenue-autopsy/chat", json={"message": "How much revenue is at risk?"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "grounded-fallback"
    assert "model not available" in payload["detail"]


def test_the_live_dataset_fits_the_provider_that_is_tried_first(transport):
    """Guards the real recovery CSV: the packet the dashboard actually builds
    must be servable, not merely trimmable."""
    from dashboard import _service

    fake = transport()
    context = build_context(_service().list_clients(), {})
    answer = _call_grounded_llm("How much revenue is at risk?", context, [])
    assert answer in {"Groq answered.", "Gemini answered."}
    sent = fake.calls[0]
    assert len(sent["prompt"]) <= PROVIDER_PROMPT_CHARS[sent["provider"]]
    assert len(_serialize(context)) > 0
