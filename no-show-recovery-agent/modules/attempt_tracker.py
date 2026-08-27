"""Persistent per-client stopping-rule and escalation tracking."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "attempts.sqlite3"
MAX_ATTEMPTS = 3


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    columns = {row[1] for row in connection.execute("PRAGMA table_info(client_attempts)")}
    if columns and "action_scope" not in columns:
        connection.execute("ALTER TABLE client_attempts RENAME TO client_attempts_legacy")
    connection.execute("CREATE TABLE IF NOT EXISTS client_attempts (client_id TEXT NOT NULL, action_scope TEXT NOT NULL DEFAULT 'payment', attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (client_id, action_scope))")
    if columns and "action_scope" not in columns:
        connection.execute("INSERT OR IGNORE INTO client_attempts (client_id, action_scope, attempt_count, updated_at) SELECT client_id, 'payment', attempt_count, updated_at FROM client_attempts_legacy")
        connection.execute("DROP TABLE client_attempts_legacy")
    connection.execute("CREATE TABLE IF NOT EXISTS escalation_flags (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0)")
    connection.execute("CREATE TABLE IF NOT EXISTS client_email_status (client_id TEXT PRIMARY KEY, current_condition TEXT NOT NULL, current_case_key TEXT NOT NULL DEFAULT '', last_email_sent_at TEXT NOT NULL, last_message_text TEXT NOT NULL)")
    email_columns = {row[1] for row in connection.execute("PRAGMA table_info(client_email_status)")}
    if "current_case_key" not in email_columns:
        connection.execute("ALTER TABLE client_email_status ADD COLUMN current_case_key TEXT NOT NULL DEFAULT ''")
    connection.commit()
    return connection


def get_attempt_count(client_id: str, db_path: Path = DB_PATH, action_scope: str = "payment") -> int:
    """Return the persisted count for one action scope (payment by default)."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    with _connect(db_path) as connection:
        row = connection.execute("SELECT attempt_count FROM client_attempts WHERE client_id = ? AND action_scope = ?", (str(client_id), action_scope)).fetchone()
    return int(row["attempt_count"]) if row else 0


def increment_attempt(client_id: str, db_path: Path = DB_PATH, action_scope: str = "payment", baseline: int = 0) -> int:
    """Increment one scoped counter, reconciling it with an external baseline."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        connection.execute(
            "INSERT INTO client_attempts (client_id, action_scope, attempt_count, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(client_id, action_scope) DO UPDATE SET attempt_count = MAX(attempt_count, ?) + 1, updated_at = excluded.updated_at",
            (str(client_id), action_scope, max(0, baseline) + 1, now, max(0, baseline)),
        )
        row = connection.execute("SELECT attempt_count FROM client_attempts WHERE client_id = ? AND action_scope = ?", (str(client_id), action_scope)).fetchone()
    return int(row["attempt_count"])


def check_escalation(client_id: str, db_path: Path = DB_PATH, action_scope: str = "payment") -> bool:
    """Return true once a scoped counter reaches the stopping-rule threshold."""
    return get_attempt_count(client_id, db_path, action_scope) >= MAX_ATTEMPTS


def flag_owner(client_id: str, reason: str, db_path: Path = DB_PATH) -> dict[str, str | int]:
    """Persist a business-owner review flag without contacting the client."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        cursor = connection.execute("INSERT INTO escalation_flags (client_id, reason, created_at) VALUES (?, ?, ?)", (str(client_id), reason, created_at))
    return {"id": int(cursor.lastrowid), "client_id": str(client_id), "reason": reason, "created_at": created_at}


def list_owner_flags(db_path: Path = DB_PATH, unresolved_only: bool = True) -> list[dict[str, str | int]]:
    """Return owner-review flags, newest first."""
    query = "SELECT * FROM escalation_flags"
    if unresolved_only:
        query += " WHERE resolved = 0"
    query += " ORDER BY id DESC"
    with _connect(db_path) as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def resolve_owner_flag(flag_id: int, db_path: Path = DB_PATH) -> bool:
    """Mark one owner-review flag resolved and report whether it existed."""
    with _connect(db_path) as connection:
        cursor = connection.execute("UPDATE escalation_flags SET resolved = 1 WHERE id = ? AND resolved = 0", (int(flag_id),))
    return cursor.rowcount == 1


def get_client_email_status(client_id: str, db_path: Path = DB_PATH) -> dict[str, str] | None:
    """Return the last confirmed email send for a client."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    with _connect(db_path) as connection:
        row = connection.execute("SELECT client_id, current_condition, current_case_key, last_email_sent_at, last_message_text FROM client_email_status WHERE client_id = ?", (str(client_id),)).fetchone()
    return dict(row) if row else None


def list_client_email_statuses(db_path: Path = DB_PATH) -> dict[str, dict[str, str]]:
    """Return confirmed email sends keyed by client ID."""
    with _connect(db_path) as connection:
        rows = connection.execute("SELECT client_id, current_condition, current_case_key, last_email_sent_at, last_message_text FROM client_email_status").fetchall()
    return {str(row["client_id"]): dict(row) for row in rows}


def record_client_email_sent(client_id: str, condition: str, message_text: str, db_path: Path = DB_PATH, case_key: str = "") -> dict[str, str]:
    """Record a send only after the delivery provider has accepted it."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    if not str(condition or "").strip():
        raise ValueError("condition is required")
    sent_at = datetime.now(timezone.utc).isoformat()
    values = (str(client_id), str(condition), str(case_key), sent_at, str(message_text))
    with _connect(db_path) as connection:
        connection.execute(
            "INSERT INTO client_email_status (client_id, current_condition, current_case_key, last_email_sent_at, last_message_text) VALUES (?, ?, ?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET current_condition = excluded.current_condition, current_case_key = excluded.current_case_key, last_email_sent_at = excluded.last_email_sent_at, last_message_text = excluded.last_message_text",
            values,
        )
    return {"client_id": values[0], "current_condition": values[1], "current_case_key": values[2], "last_email_sent_at": values[3], "last_message_text": values[4]}
