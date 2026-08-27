"""Transactional audit store with a CSV projection for compatibility."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "logs" / "audit_log.csv"
FIELDS = ["timestamp", "client_id", "client_name", "event_type", "source", "action", "message", "payment_status", "outcome", "status", "errors", "event_json"]


def audit_db_path(audit_path: Path = AUDIT_PATH) -> Path:
    """Return the transactional SQLite store paired with a CSV projection."""
    return audit_path.with_suffix(".sqlite3")


def _export_csv(connection: sqlite3.Connection, audit_path: Path) -> None:
    rows = connection.execute("SELECT " + ", ".join(FIELDS) + " FROM audit_events ORDER BY id").fetchall()
    with audit_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)


def log_event(event: dict[str, Any], action: str, message: str | None, payment_status: str | None, audit_path: Path = AUDIT_PATH, errors: list[str] | None = None, outcome: str | None = None) -> dict[str, str]:
    """Insert one audit row transactionally and refresh the CSV read projection."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = audit_db_path(audit_path)
    combined_errors = list(event.get("validation_errors") or []) + list(errors or [])
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        "event_json": json.dumps(event, default=str, sort_keys=True),
    }
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, " + ", ".join(f"{field} TEXT NOT NULL" for field in FIELDS) + ")")
        connection.execute("INSERT INTO audit_events (" + ", ".join(FIELDS) + ") VALUES (" + ", ".join("?" for _ in FIELDS) + ")", tuple(row[field] for field in FIELDS))
        connection.commit()
        _export_csv(connection, audit_path)
    return row
