"""Grounded conversational analysis for Revenue Autopsy AI.

The CSV business records and the dashboard projection are packaged as structured
evidence and handed to the LLM alongside one comprehensive system prompt. All
question interpretation — revenue leak investigations, dashboard/email status,
client lookups, breakdowns, counts, casual conversation, everything — is done by
the model reading that prompt plus the evidence, not by keyword/condition logic
in this file. A minimal non-interpretive fallback is used only if no LLM can be
reached at all.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
CONVERSATION_DB = DATA_DIR / "revenue_autopsy.sqlite3"

# Load project credentials regardless of the directory used to launch Flask.
# Existing process environment values take precedence in deployed environments.
load_dotenv(dotenv_path=ENV_PATH, override=False)

SYSTEM_PROMPT = """You are the AI Revenue Recovery Analyst embedded in this SaaS's operations dashboard. You are the primary interface staff use to interrogate revenue-recovery data, so you must be able to answer any question the supplied evidence can support — not just a fixed set of report types. All question interpretation is your job: there is no separate rules engine deciding what kind of question this is. Read the question, read the evidence below, and answer it as well as the evidence allows.

═══════════════════════════
YOUR DATA
═══════════════════════════
Every message includes a CURRENT AUTHORIZED DATA CONTEXT block: a JSON object with —
- generated_at: when this context was built
- sources: which CSV files were loaded (empty list if none)
- filters: any filters currently applied by the caller
- metrics: pre-aggregated figures you can use directly — csv_record_count, dashboard_client_count, value_at_risk, recovered_value, resolved_clients, unresolved_records, emailed_count, not_emailed_count, failure_reasons (list of {reason, count, amount}), case_types (list of {type, count, amount}), conditions (list of {condition, count, amount})
- csv_records: raw rows from uploaded CSV exports — schema varies by file, but commonly includes subscription_amount, appointment_value, fee_amount, invoice_amount, payment_status, outcome, invoice_status, failure_reason, case_type, event_type, attempt_count, client_id, client_name, last_charge_date, and other columns
- dashboard_records: one row per dashboard client — client_id, client_name, client_email, condition, payment_status, outcome, email_sent (bool), last_email_sent_at, amount, case (nested raw case object), audit_trail (list of past actions/events for that client)
- evidence_scope: how complete the record lists are for THIS request. When complete is false, csv_records/dashboard_records hold only the highest-amount subset that fit the request: metrics still covers every record and remains authoritative for totals, counts and breakdowns, but you must not state or imply the listed rows are the complete set, and if the question needs an omitted row you say plainly that the record list was trimmed.

This data — and nothing else — is the only source of truth for any business, financial, or client-status claim you make. You have no access to any other system, and no memory beyond the conversation history included in this request.

═══════════════════════════
WHAT YOU CAN BE ASKED — ANSWER ALL OF IT DIRECTLY
═══════════════════════════
- Revenue leak investigations: what's at risk, why, how much, which customers, what to do about it
- Unpaid, failed, or unresolved customers and transactions — in total, or filtered by reason, amount, condition, or attempt count
- Recovered or resolved revenue, and which customers it came from
- Recovery prioritization and ranking — state your ranking logic in plain language (e.g. easily-fixable reasons first, fewer prior attempts first, larger amounts as a tiebreaker) and label the result an estimate, not a guaranteed probability
- Failure-reason, case-type, and condition breakdowns, from CSV records, dashboard records, or both
- Email / outreach status: who has or hasn't been emailed, when, and what that implies for follow-up
- Audit-trail or history questions about a specific client — summarize that client's audit_trail entries in plain language, in chronological order
- Individual client lookups by name or ID — pull every relevant field for that one client and nothing else
- Counts, sums, averages, percentages, rankings, and comparisons across any dimension present in the data
- Data provenance questions ("what data are you using", "where does this come from", "how fresh is this")
- Ordinary conversation — greetings, thanks, "what can you do", clarifications, and follow-ups that reference earlier turns
- Anything else the data can genuinely support. Use judgment; don't wait for a question to match a template before answering it.

═══════════════════════════
WHAT YOU MUST NOT DO
═══════════════════════════
- Never invent a customer, transaction, amount, date, reason, cause, email status, audit entry, or statistic that isn't in the supplied data
- Never claim a recovery action (email sent, retry triggered, refund issued, etc.) was executed unless an authorized tool result confirms it — describe what should happen, never that it has happened
- Never present a failure_reason as a proven root cause — it's a label the payment system recorded, not verified causation
- Never fabricate a period-over-period comparison if the data lacks complete, comparable figures for both periods — say plainly what's missing instead
- Don't default to "insufficient data" as a deflection — only say it when the specific fields the question needs are genuinely absent, and say exactly what's missing

═══════════════════════════
HOW TO RESPOND
═══════════════════════════
Match the length and shape of your answer to the question. This is the single most important rule: never pad, never over-structure, never spend words the question didn't ask for.

- Right-size every answer:
  • A count/sum/lookup/yes-no question → answer in one sentence, leading with the number or fact. No headings, no preamble, no closing summary. Example: "23 customers haven't paid, totaling ₹4,82,000."
  • A greeting, thanks, or "what can you do" → one or two natural sentences. Never turn it into a data report.
  • A breakdown or ranking → a short lead line plus a tight list; only as many items as asked for (respect "top 5", "show 10"), otherwise 3–7.
  • Only a genuine, open-ended revenue-leak investigation earns the full structure: Revenue Leak Autopsy / Finding / Financial Impact / Root Cause / Evidence / Affected Customers/Transactions / Recovery Opportunity / Recommended Action. Do not use this template for anything smaller.
- Lead with the answer, then justify if needed — never make the reader wade through context to reach the point. Cut throat-clearing like "Based on the data provided…", "Great question", "Let me analyze…".
- Prefer plain, concrete numbers and client_ids over adjectives. Every customer-level fact cites its exact client_id so it can be traced back.
- Use **Fact / Pattern / Estimate / Recommendation** labels only when the distinction genuinely matters (mainly investigations and recommendations). Don't label a one-line count.
- Format currency in ₹ with thousands separators.
- Handle every query type on its own terms — data questions, provenance questions, follow-ups that lean on earlier turns, and off-topic or unanswerable asks alike. If the data can't support the question, say so in one sentence and state exactly which field is missing; never deflect with a generic "insufficient data".
- When a question is ambiguous, make the most reasonable interpretation and answer it, noting the assumption in a short clause — don't stall with a clarifying question unless answering is truly impossible.
- Default to brevity. A shorter correct answer is always better than a longer one that says the same thing."""


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _amount(record: dict[str, Any]) -> float:
    for key in ("amount", "subscription_amount", "appointment_value", "fee_amount", "invoice_amount"):
        value = _number(record.get(key))
        if value:
            return value
    return 0.0


def _resolved(record: dict[str, Any]) -> bool:
    values = {str(record.get(key) or "").lower() for key in ("payment_status", "outcome", "invoice_status")}
    return bool(values & {"paid", "recovered", "resolved"})


# ─── Provider capacity ──────────────────────────────────────────────────────
# Prompt ceilings in characters for the WHOLE request — system prompt, history
# and evidence together, not the evidence alone.
#
# Groq's on-demand tier caps tokens-per-minute far below any model's own
# context window, so a full-size autopsy packet is refused outright with
# HTTP 413 ``rate_limit_exceeded`` ("Limit 8000, Requested 35854") no matter
# which model is selected. At roughly four characters per token, 8,000 TPM
# leaves about 24,000 prompt characters once room is reserved for the answer.
# Gemini's ceiling is a request-size concern rather than a rate limit.
PROVIDER_PROMPT_CHARS = {"Groq": 24000, "Gemini": 600000}

# Model ids verified against ListModels for the configured key. The alias is
# used only when the pinned id is not visible to whichever key is in play.
GEMINI_ANALYST_MODEL = "gemini-3.6-flash"
GEMINI_ANALYST_FALLBACK = "gemini-flash-latest"

# How many raw records to keep while shrinking a packet to fit a ceiling.
RECORD_STEPS = (400, 200, 100, 50, 25, 10, 0)
AUDIT_TRAIL_KEEP = 3
HISTORY_TURNS = 12


def _redact(text: Any, limit: int = 220) -> str:
    """Collapse a provider error to one safe, short line.

    Provider errors quote the request URL, and the Gemini endpoint carries the
    API key in its query string, so an unredacted message would publish a live
    credential onto the dashboard the moment a request failed.
    """
    cleaned = re.sub(r"(?i)\b(key|api[-_]?key|token|authorization)=[^&\s\"']+", r"\1=REDACTED", str(text or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _prompt_budget(name: str) -> int:
    """Prompt-character ceiling for one provider, overridable per deployment."""
    try:
        override = int(str(os.getenv(f"{name.upper()}_ANALYST_PROMPT_CHARS") or "").strip())
    except ValueError:
        override = 0
    return override if override > 0 else PROVIDER_PROMPT_CHARS.get(name, 24000)


def _scope(csv_included: int, csv_total: int, dash_included: int, dash_total: int) -> dict[str, Any]:
    """State how complete the record lists are for one request.

    Carried inside the evidence itself so the analyst can never mistake a
    trimmed subset for the whole file and quietly understate the book.
    """
    complete = csv_included >= csv_total and dash_included >= dash_total
    return {
        "complete": complete,
        "csv_records_included": csv_included, "csv_records_total": csv_total,
        "dashboard_records_included": dash_included, "dashboard_records_total": dash_total,
        "note": "Record lists hold every row for this request." if complete else (
            f"Record lists were trimmed to the highest-amount rows to fit this request, and each dashboard row "
            f"keeps only its {AUDIT_TRAIL_KEEP} most recent audit entries. metrics still covers ALL records and is "
            f"authoritative for totals, counts and breakdowns. Never state or imply the listed rows are the complete "
            f"set; if the question needs an omitted row, say the record list was trimmed."
        ),
    }


def _slim(record: dict[str, Any]) -> dict[str, Any]:
    """Drop the unbounded part of a dashboard row: its full audit history."""
    trail = record.get("audit_trail")
    if not isinstance(trail, list) or len(trail) <= AUDIT_TRAIL_KEEP:
        return record
    return {**record, "audit_trail": trail[-AUDIT_TRAIL_KEEP:]}


def _serialize(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":"))


def _messages(question: str, evidence: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[-HISTORY_TURNS:],
        {"role": "user", "content": f"CURRENT AUTHORIZED DATA CONTEXT:\n{evidence}\n\nUSER QUESTION:\n{question}"},
    ]


def _overhead(question: str, history: list[dict[str, str]]) -> int:
    """Characters every request spends before any evidence is added."""
    fixed = len(SYSTEM_PROMPT) + len(question) + 64
    return fixed + sum(len(str(item.get("content") or "")) for item in history[-HISTORY_TURNS:])


def fit_context(context: dict[str, Any], budget_chars: int, overhead_chars: int = 0) -> tuple[dict[str, Any], str]:
    """Return the largest version of the evidence packet that fits a budget.

    Pre-aggregated ``metrics`` are never trimmed — they are the authoritative
    totals and cost almost nothing. The raw record lists are what grow without
    bound as the operator's CSV grows, so they are shed progressively: first
    each row's audit history, then the rows themselves, highest exposure kept
    first. Every trimmed packet states what it omitted via ``evidence_scope``.

    Returns the packet and its serialization. The serialization may still
    exceed the budget when even metrics-only evidence does not fit, which the
    caller treats as "this provider cannot serve this question".
    """
    room = max(budget_chars - overhead_chars, 0)
    full = _serialize(context)
    if len(full) <= room:
        return context, full

    csv_ranked = sorted(context.get("csv_records") or [], key=_amount, reverse=True)
    dash_ranked = sorted(context.get("dashboard_records") or [], key=_amount, reverse=True)
    trimmed, evidence = context, full
    for keep in RECORD_STEPS:
        csv_kept = csv_ranked[:keep]
        dash_kept = [_slim(row) for row in dash_ranked[:keep]]
        trimmed = {
            **context, "csv_records": csv_kept, "dashboard_records": dash_kept,
            "evidence_scope": _scope(len(csv_kept), len(csv_ranked), len(dash_kept), len(dash_ranked)),
        }
        evidence = _serialize(trimmed)
        if len(evidence) <= room:
            break
    return trimmed, evidence


def _canonical_csv_files(data_dir: Path) -> list[Path]:
    """Return the single merged recovery source, and only that source.

    The dashboard runs exclusively on the one combined CSV the operator uploads
    (``recovery_cases.csv``), where each row is tagged by ``case_type``. Split
    exports such as ``no_show_cases.csv`` or ``failed_subscription_cases.csv``
    are intentionally never read — there is one file, one source of truth.
    """
    combined = data_dir / "recovery_cases.csv"
    return [combined] if combined.exists() else []


def load_csv_records(data_dir: Path = DATA_DIR) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    sources: list[str] = []
    for path in _canonical_csv_files(data_dir):
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row_number, row in enumerate(csv.DictReader(handle), start=2):
                    cleaned = {str(key): value for key, value in row.items() if key is not None}
                    cleaned["_source"] = path.name
                    cleaned["_row"] = row_number
                    records.append(cleaned)
            sources.append(path.name)
        except (OSError, csv.Error):
            continue
    return records, sources


def build_context(clients: list[dict[str, Any]], filters: dict[str, Any] | None = None, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Assemble the full evidence packet — raw records plus pre-aggregated metrics
    — that gets serialized straight into the LLM prompt."""
    csv_records, sources = load_csv_records(data_dir)
    dashboard_records: list[dict[str, Any]] = []
    for client in clients:
        case = client.get("case") if isinstance(client.get("case"), dict) else {}
        dashboard_records.append({
            "client_id": client.get("client_id"), "client_name": client.get("name"),
            "client_email": client.get("email"), "condition": client.get("condition"),
            "payment_status": client.get("payment_status"), "outcome": client.get("outcome"),
            "email_sent": bool(client.get("email_sent")), "last_email_sent_at": client.get("last_email_sent_at"),
            "amount": _amount(case), "case": case, "audit_trail": client.get("audit_trail") or [],
        })
    source_records = csv_records or dashboard_records
    at_risk = [record for record in source_records if not _resolved(record)]
    resolved_dashboard = [record for record in dashboard_records if _resolved(record)]
    reasons: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})
    types: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})
    conditions: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for record in at_risk:
        reason = str(record.get("failure_reason") or "not_recorded")
        reasons[reason]["count"] += 1
        reasons[reason]["amount"] += _amount(record)
        case_type = str(record.get("case_type") or record.get("event_type") or record.get("condition") or "unknown")
        types[case_type]["count"] += 1
        types[case_type]["amount"] += _amount(record)
    for record in dashboard_records:
        condition = str(record.get("condition") or "").strip()
        if condition:
            conditions[condition]["count"] += 1
            conditions[condition]["amount"] += _amount(record)
    emailed = [record for record in dashboard_records if record.get("email_sent")]
    metrics = {
        "csv_record_count": len(csv_records), "dashboard_client_count": len(dashboard_records),
        "value_at_risk": round(sum(_amount(row) for row in at_risk), 2),
        "recovered_value": round(sum(_amount(row) for row in resolved_dashboard), 2),
        "resolved_clients": len(resolved_dashboard), "unresolved_records": len(at_risk),
        "emailed_count": len(emailed), "not_emailed_count": len(dashboard_records) - len(emailed),
        "failure_reasons": [{"reason": key, **value} for key, value in sorted(reasons.items(), key=lambda item: (-item[1]["amount"], item[0]))],
        "case_types": [{"type": key, **value} for key, value in sorted(types.items(), key=lambda item: (-item[1]["amount"], item[0]))],
        "conditions": [{"condition": key, **value} for key, value in sorted(conditions.items(), key=lambda item: (-item[1]["amount"], item[0]))],
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "sources": sources, "filters": filters or {},
        "evidence_scope": _scope(len(csv_records), len(csv_records), len(dashboard_records), len(dashboard_records)),
        "metrics": metrics, "csv_records": csv_records, "dashboard_records": dashboard_records,
    }


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_autopsy_conversation ON messages(conversation_id, id)")


def conversation_history(conversation_id: str, db_path: Path = CONVERSATION_DB, limit: int = 20) -> list[dict[str, str]]:
    _init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def _store(conversation_id: str, role: str, content: str, db_path: Path) -> None:
    _init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)", (conversation_id, role, content))


def _inr(value: float) -> str:
    return f"₹{value:,.0f}"


def deterministic_answer(question: str, context: dict[str, Any], history: list[dict[str, str]], reason: str = "") -> tuple[str, list[str]]:
    """Used only when no LLM analyst could be reached at all. This is intentionally
    not question-aware — no keyword or condition matching — because interpreting
    the question is the LLM's job, driven entirely by SYSTEM_PROMPT. This just
    surfaces the grounded data snapshot so the caller still gets something truthful
    while the AI analyst is unavailable.

    ``reason`` carries the redacted provider failure through to the operator.
    Without it the dashboard shows an unexplained snapshot and the actual cause
    — a rejected model id, an exhausted rate limit, an invalid key — is visible
    nowhere in the product.
    """
    metrics = context["metrics"]
    lines = [
        "The AI analyst is temporarily unavailable, so here is the current grounded data snapshot instead of an interpreted answer:",
        "",
        f"- CSV records: {metrics['csv_record_count']} (sources: {', '.join(context['sources']) or 'none loaded'})",
        f"- Dashboard clients: {metrics['dashboard_client_count']}",
        f"- Unresolved records: {metrics['unresolved_records']} ({_inr(metrics['value_at_risk'])} at risk)",
        f"- Resolved clients: {metrics['resolved_clients']} ({_inr(metrics['recovered_value'])} recovered)",
        f"- Emailed: {metrics['emailed_count']} / {metrics['dashboard_client_count']} dashboard clients (not yet contacted: {metrics['not_emailed_count']})",
    ]
    if metrics["failure_reasons"]:
        top = metrics["failure_reasons"][0]
        lines.append(f"- Top failure reason: {top['reason'].replace('_', ' ')} — {top['count']} records, {_inr(top['amount'])}")
    lines.append("")
    if reason:
        lines.append(f"Reason the AI analyst could not answer: {reason}")
    elif os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY"):
        lines.append("The configured AI providers could not complete this request. Check provider availability, model access, and API-key validity, then retry.")
    else:
        lines.append("Configure GROQ_API_KEY or GEMINI_API_KEY in the project .env file so the analyst can answer this specific question directly.")
    return ("\n".join(lines), [])


def _groq_answer(messages: list[dict[str, str]]) -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("not configured")
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": os.getenv("GROQ_ANALYST_MODEL", os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")), "messages": messages, "temperature": 0.2},
        timeout=45,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} {_redact(response.text)}")
    answer = str(response.json()["choices"][0]["message"]["content"]).strip()
    if not answer:
        raise RuntimeError("empty response")
    return answer


def _gemini_answer(messages: list[dict[str, str]]) -> str:
    """Ask Gemini, retrying once on a model the configured key cannot see.

    A pinned model id is right for reproducibility but wrong when the key in a
    given deployment has no access to it — that returns HTTP 404 and used to
    kill the whole provider. The published alias is tried second so a key with
    a different model catalogue still answers.
    """
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("not configured")
    prompt = "\n\n".join(f"{item['role'].upper()}: {item['content']}" for item in messages)
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
    configured = str(os.getenv("GEMINI_ANALYST_MODEL") or "").strip() or GEMINI_ANALYST_MODEL
    failures: list[str] = []
    for model in list(dict.fromkeys([configured, GEMINI_ANALYST_FALLBACK])):
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json=body, timeout=60,
        )
        if response.status_code == 404:
            failures.append(f"{model}: model not available to this key")
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"{model}: HTTP {response.status_code} {_redact(response.text)}")
        answer = str(response.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
        if not answer:
            raise RuntimeError(f"{model}: empty response")
        return answer
    raise RuntimeError("; ".join(failures) or "no model available")


def _call_grounded_llm(question: str, context: dict[str, Any], history: list[dict[str, str]]) -> str:
    """Send the grounded packet to whichever configured provider can carry it.

    Each provider has its own prompt ceiling, so each gets its own version of
    the evidence, shrunk only as far as that ceiling requires. Providers that
    can carry the evidence untrimmed are tried first, so a rate-limited tier is
    never allowed to answer a question from a truncated book while a
    larger-capacity provider sits unused behind it. Among providers that can
    carry it all, the smallest sufficient ceiling wins, which keeps the fast
    tier primary for ordinary questions. Among providers that must trim, the
    largest ceiling wins, because it trims least. A provider too small even for
    metrics-only evidence is skipped rather than sent a request that is certain
    to be refused.
    """
    load_dotenv(dotenv_path=ENV_PATH, override=False)
    overhead = _overhead(question, history)
    callers: dict[str, Callable[[list[dict[str, str]]], str]] = {"Groq": _groq_answer, "Gemini": _gemini_answer}
    errors: list[str] = []
    candidates: list[tuple[int, int, str, str]] = []
    for name in callers:
        if not os.getenv(f"{name.upper()}_API_KEY"):
            continue
        budget = _prompt_budget(name)
        packet, evidence = fit_context(context, budget, overhead)
        if len(evidence) + overhead > budget:
            errors.append(f"{name}: prompt ceiling of {budget} characters is too small for this request")
            continue
        complete = bool(packet.get("evidence_scope", {}).get("complete", True))
        candidates.append((0 if complete else 1, budget if complete else -budget, name, evidence))

    for _, _, name, evidence in sorted(candidates):
        try:
            return callers[name](_messages(question, evidence, history))
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, RuntimeError) as exc:
            errors.append(f"{name}: {_redact(exc)}")
    if errors:
        raise RuntimeError("All configured analyst providers failed: " + " | ".join(errors))
    raise RuntimeError("No analyst LLM is configured")


def analyze(question: str, clients: list[dict[str, Any]], conversation_id: str | None = None, filters: dict[str, Any] | None = None, db_path: Path = CONVERSATION_DB, llm: Callable[[str, dict[str, Any], list[dict[str, str]]], str] | None = None) -> dict[str, Any]:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise ValueError("message is required")
    if len(clean_question) > 4000:
        raise ValueError("message must be 4000 characters or fewer")
    identifier = conversation_id or str(uuid.uuid4())
    history = conversation_history(identifier, db_path)
    context = build_context(clients, filters)
    analyst = llm or _call_grounded_llm
    mode = "ai"
    detail = ""
    try:
        answer = analyst(clean_question, context, history)
        if not answer:
            raise RuntimeError("Analyst returned an empty answer")
        all_records = context["csv_records"] + context["dashboard_records"]
        cited_ids = [str(row.get("client_id")) for row in all_records if str(row.get("client_id") or "") and str(row.get("client_id")) in answer]
    except Exception as exc:
        detail = _redact(exc, 600) or exc.__class__.__name__
        answer, cited_ids = deterministic_answer(clean_question, context, history, detail)
        mode = "grounded-fallback"
    _store(identifier, "user", clean_question, db_path)
    _store(identifier, "assistant", answer, db_path)
    return {"conversation_id": identifier, "answer": answer, "mode": mode, "detail": detail, "cited_client_ids": list(dict.fromkeys(cited_ids)), "context": {"generated_at": context["generated_at"], "sources": context["sources"], "csv_record_count": context["metrics"]["csv_record_count"], "dashboard_client_count": context["metrics"]["dashboard_client_count"], "filters": context["filters"]}}