"""Immutable audit trail: SQLite store of record + CSV/JSON read projections.

What this file guarantees
------------------------
Every row is append-only. There is no update or delete path in this module —
``log_event`` only ever INSERTs. The CSV and JSON files are *projections*
regenerated from the store, never the source of truth, so hand-editing a CSV
cannot rewrite history.

One row captures the full decision chain the track asks for:

    detected_at → diagnosis → policy verdict + reason → action → outcome
                → timestamps → actor

Columns are grouped accordingly:

    identity   timestamp, detected_at, client_id, client_name, event_type, source
    diagnosis  root_cause, diagnosis_source, diagnosis_confidence
    policy     decision, reason_code, reason, idempotency_key, attempt_number,
               max_attempts, contact_window_ok, next_attempt_at, policy_badge
    action     action, message, payment_status
    outcome    outcome, status, errors, actor
    payload    event_json

``log_event`` keeps its original positional signature so existing callers and
tests continue to work; the decision-chain columns arrive through the optional
``verdict``/``diagnosis``/``actor`` keyword arguments and default to empty.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "logs" / "audit_log.csv"

# The original twelve columns stay first and keep their names so existing
# dashboard readers, metrics code, and tests are unaffected.
LEGACY_FIELDS = [
    "timestamp",
    "client_id",
    "client_name",
    "event_type",
    "source",
    "action",
    "message",
    "payment_status",
    "outcome",
    "status",
    "errors",
    "event_json",
]

# Decision-chain columns appended for the auditable pipeline.
POLICY_FIELDS = [
    "detected_at",
    "root_cause",
    "diagnosis_source",
    "diagnosis_confidence",
    "decision",
    "reason_code",
    "reason",
    "idempotency_key",
    "attempt_number",
    "max_attempts",
    "contact_window_ok",
    "next_attempt_at",
    "policy_badge",
    "actor",
]

FIELDS = LEGACY_FIELDS + POLICY_FIELDS


def audit_db_path(audit_path: Path = AUDIT_PATH) -> Path:
    """Return the transactional SQLite store paired with a CSV projection."""
    return audit_path.with_suffix(".sqlite3")


def audit_json_path(audit_path: Path = AUDIT_PATH) -> Path:
    """Return the JSON projection path paired with the CSV projection."""
    return audit_path.with_suffix(".json")


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the append-only table, adding any columns a older file lacks."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in FIELDS)
        + ")"
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
    for field in FIELDS:
        if field not in existing:
            connection.execute(f"ALTER TABLE audit_events ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")


def _export_csv(connection: sqlite3.Connection, audit_path: Path) -> None:
    rows = connection.execute("SELECT " + ", ".join(FIELDS) + " FROM audit_events ORDER BY id").fetchall()
    with audit_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)


def _export_json(connection: sqlite3.Connection, audit_path: Path) -> None:
    """Write the JSON projection: one object per case, event payload parsed."""
    rows = connection.execute(
        "SELECT id, " + ", ".join(FIELDS) + " FROM audit_events ORDER BY id"
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {key: row[key] for key in FIELDS}
        record["id"] = row["id"]
        try:
            record["event"] = json.loads(record.pop("event_json") or "{}")
        except (TypeError, ValueError):
            record["event"] = {}
        records.append(record)
    audit_json_path(audit_path).write_text(
        json.dumps(records, indent=2, sort_keys=False, ensure_ascii=False),
        encoding="utf-8",
    )


def _verdict_columns(verdict: Any) -> dict[str, str]:
    """Flatten a PolicyVerdict (or its dict form) into audit columns."""
    if verdict is None:
        return {}
    data = verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict)
    contact_ok = data.get("contact_window_ok")
    return {
        "decision": str(data.get("decision") or ""),
        "reason_code": str(data.get("reason_code") or ""),
        "reason": str(data.get("reason") or ""),
        "idempotency_key": str(data.get("idempotency_key") or ""),
        "attempt_number": "" if data.get("attempt_number") is None else str(data.get("attempt_number")),
        "max_attempts": "" if data.get("max_attempts") is None else str(data.get("max_attempts")),
        "contact_window_ok": "" if contact_ok is None else ("true" if contact_ok else "false"),
        "next_attempt_at": str(data.get("next_attempt_at") or ""),
        "policy_badge": str(data.get("badge") or (verdict.badge() if hasattr(verdict, "badge") else "")),
    }


def _diagnosis_columns(diagnosis: dict[str, Any] | None) -> dict[str, str]:
    """Flatten a typed diagnosis payload into audit columns (never its PII)."""
    if not diagnosis:
        return {}
    confidence = diagnosis.get("confidence")
    return {
        "root_cause": str(diagnosis.get("root_cause") or ""),
        "diagnosis_source": str(diagnosis.get("source") or ""),
        "diagnosis_confidence": "" if confidence is None else f"{float(confidence):.2f}",
    }


def log_event(
    event: dict[str, Any],
    action: str,
    message: str | None,
    payment_status: str | None,
    audit_path: Path = AUDIT_PATH,
    errors: list[str] | None = None,
    outcome: str | None = None,
    verdict: Any = None,
    diagnosis: dict[str, Any] | None = None,
    actor: str = "agent",
) -> dict[str, str]:
    """Append one immutable audit row and refresh the CSV/JSON projections."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = audit_db_path(audit_path)
    combined_errors = list(event.get("validation_errors") or []) + list(errors or [])
    row = {field: "" for field in FIELDS}
    row.update(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detected_at": str(event.get("detected_at") or event.get("occurred_at") or ""),
            "client_id": str(event.get("client_id") or ""),
            "client_name": str(event.get("client_name") or ""),
            "event_type": str(event.get("event_type") or ""),
            "source": str(event.get("source") or ""),
            "action": action,
            "message": message or "",
            "payment_status": payment_status or "not_applicable",
            "outcome": outcome or ("technical_error" if combined_errors else "action_completed"),
            "status": "flagged_error" if combined_errors else "clean",
            "errors": "; ".join(combined_errors),
            "actor": actor or "agent",
            "event_json": json.dumps(event, default=str, sort_keys=True),
        }
    )
    row.update(_diagnosis_columns(diagnosis))
    row.update(_verdict_columns(verdict))
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        _ensure_schema(connection)
        connection.execute(
            "INSERT INTO audit_events (" + ", ".join(FIELDS) + ") VALUES (" + ", ".join("?" for _ in FIELDS) + ")",
            tuple(row[field] for field in FIELDS),
        )
        connection.commit()
        _export_csv(connection, audit_path)
        _export_json(connection, audit_path)
    return row


def read_events(audit_path: Path = AUDIT_PATH) -> list[dict[str, str]]:
    """Return every audit row from the store of record, oldest first."""
    db_path = audit_db_path(audit_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
        rows = connection.execute("SELECT " + ", ".join(FIELDS) + " FROM audit_events ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def export_trail(audit_path: Path = AUDIT_PATH) -> dict[str, str]:
    """Regenerate both projections from the store; return the written paths."""
    db_path = audit_db_path(audit_path)
    if not db_path.exists():
        return {"csv": "", "json": ""}
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
        _export_csv(connection, audit_path)
        _export_json(connection, audit_path)
    return {"csv": str(audit_path), "json": str(audit_json_path(audit_path))}


if __name__ == "__main__":
    import tempfile

    # ignore_cleanup_errors: SQLite WAL handles can outlive the block on Windows.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "audit.csv"
        failures = 0

        def check(label: str, condition: bool) -> None:
            global failures
            failures += 0 if condition else 1
            print(f"{'PASS' if condition else 'FAIL'} {label}")

        verdict_stub = {
            "decision": "approve",
            "reason_code": "auto_approved",
            "reason": "Auto-approved: confidence 0.88 at or above 0.75 threshold",
            "idempotency_key": "pol_test123",
            "attempt_number": 1,
            "max_attempts": 3,
            "contact_window_ok": True,
            "next_attempt_at": None,
            "badge": "Attempt 1 of 3 • Contact window OK • Escalates after attempt 3",
        }
        diagnosis_stub = {"root_cause": "card_expired", "confidence": 0.88, "source": "heuristic"}
        first = log_event(
            {"client_id": "C1", "client_name": "Asha", "event_type": "payment_failed", "source": "webhook", "detected_at": "2026-09-01T05:00:00+00:00"},
            "resend_payment_link",
            "Your card on file expired.",
            "link_created",
            path,
            outcome="action_completed",
            verdict=verdict_stub,
            diagnosis=diagnosis_stub,
            actor="agent",
        )
        check("decision chain recorded on the row", first["decision"] == "approve" and first["reason_code"] == "auto_approved")
        check("diagnosis columns recorded", first["root_cause"] == "card_expired" and first["diagnosis_confidence"] == "0.88")
        check("badge carried into the trail", "Attempt 1 of 3" in first["policy_badge"])
        check("actor recorded", first["actor"] == "agent")

        log_event(
            {"client_id": "C2", "client_name": "Ravi", "event_type": "payment_failed", "source": "webhook"},
            "escalate_human",
            None,
            "not_applicable",
            path,
            outcome="human_review",
            verdict={"decision": "escalate", "reason_code": "amount_above_threshold", "reason": "Amount INR 64,000 above INR 50,000 auto-action ceiling", "idempotency_key": "pol_test456", "attempt_number": 1, "max_attempts": 3, "contact_window_ok": True, "badge": "Attempt 1 of 3 • Contact window OK • Human review"},
            diagnosis={"root_cause": "insufficient_funds", "confidence": 0.82, "source": "llm"},
            actor="agent",
        )

        with path.open(newline="", encoding="utf-8") as handle:
            csv_rows = list(csv.DictReader(handle))
        check("csv projection holds both rows", len(csv_rows) == 2)
        check("legacy columns still lead the csv", list(csv_rows[0])[:12] == LEGACY_FIELDS)
        check("escalation reason is visible in the csv", csv_rows[1]["reason"].startswith("Amount INR 64,000"))

        json_rows = json.loads(audit_json_path(path).read_text(encoding="utf-8"))
        check("json projection holds both rows", len(json_rows) == 2)
        check("json parses the event payload", json_rows[0]["event"]["client_id"] == "C1")
        check("json keeps the reason code", json_rows[1]["reason_code"] == "amount_above_threshold")

        stored = read_events(path)
        check("store of record returns rows in order", [row["client_id"] for row in stored] == ["C1", "C2"])

        legacy_only = log_event(
            {"client_id": "C3", "event_type": "no_show", "source": "recovery_cases.csv"},
            "friendly_reminder",
            "See you next time.",
            "not_applicable",
            path,
        )
        check("legacy call still works with empty policy columns", legacy_only["decision"] == "" and legacy_only["reason_code"] == "")

        exported = export_trail(path)
        check("export regenerates both projections", exported["csv"].endswith(".csv") and exported["json"].endswith(".json"))

        if failures:
            raise SystemExit(1)
