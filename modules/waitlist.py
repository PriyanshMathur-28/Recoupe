"""SQLite-backed waitlist management."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .message_generator import generate_message
from .messenger import send_message

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "waitlist.sqlite3"


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS waitlist (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, client_name TEXT NOT NULL, client_email TEXT NOT NULL, date_added TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting')")
    connection.commit()
    return connection


def update_waitlist_entry(entry_id: int, client: dict[str, Any], db_path: Path = DB_PATH) -> dict[str, Any]:
    """Update a waitlist row while preserving its FIFO insertion timestamp."""
    client_id = str(client.get("client_id") or "").strip()
    client_name = str(client.get("client_name") or "").strip()
    client_email = str(client.get("client_email") or "").strip()
    if not client_id or not client_name or "@" not in client_email:
        raise ValueError("client_id, client_name, and valid client_email are required")
    with _connect(db_path) as connection:
        cursor = connection.execute(
            "UPDATE waitlist SET client_id = ?, client_name = ?, client_email = ?, status = COALESCE(NULLIF(?, ''), status) WHERE id = ?",
            (client_id, client_name, client_email, str(client.get("status") or ""), int(entry_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError("Waitlist entry not found")
        row = connection.execute("SELECT * FROM waitlist WHERE id = ?", (int(entry_id),)).fetchone()
    return dict(row)


def list_waitlist(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Return waitlist rows in FIFO order for operational editing."""
    with _connect(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM waitlist ORDER BY datetime(date_added), id").fetchall()]


def add_to_waitlist(client: dict[str, Any], db_path: Path = DB_PATH) -> dict[str, Any]:
    client_id = str(client.get("client_id") or "").strip()
    client_name = str(client.get("client_name") or "").strip()
    client_email = str(client.get("client_email") or "").strip()
    required = [client_id, client_name, client_email]
    if any(not value for value in required) or "@" not in client_email:
        raise ValueError("client_id, client_name, and valid client_email are required")
    with _connect(db_path) as connection:
        cursor = connection.execute("INSERT INTO waitlist (client_id, client_name, client_email, date_added, status) VALUES (?, ?, ?, ?, 'waiting')", (*required, datetime.now(timezone.utc).isoformat()))
        row = connection.execute("SELECT * FROM waitlist WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def get_next_in_line(db_path: Path = DB_PATH) -> dict[str, Any] | None:
    with _connect(db_path) as connection:
        row = connection.execute("SELECT * FROM waitlist WHERE status = 'waiting' ORDER BY datetime(date_added), id LIMIT 1").fetchone()
    return dict(row) if row else None


def has_waiting_entry(db_path: Path = DB_PATH) -> bool:
    """Return whether a real FIFO waitlist recipient is currently available."""
    return get_next_in_line(db_path) is not None


def notify_waitlist_person(slot_info: dict[str, Any], db_path: Path = DB_PATH, service: Any = None, llm: Any = None) -> dict[str, Any]:
    person = get_next_in_line(db_path)
    if person is None:
        raise LookupError("No waiting client is available")
    event = {**person, **slot_info}
    body = generate_message(event, "offer_waitlist", llm=llm)
    result = send_message(person["client_email"], "Appointment slot available", body, service=service)
    with _connect(db_path) as connection:
        connection.execute("UPDATE waitlist SET status = 'notified' WHERE id = ?", (person["id"],))
    return {"person": person, "message": body, "delivery": result}


def mark_slot(status: str, db_path: Path = DB_PATH) -> str:
    if status not in {"open", "filled"}:
        raise ValueError("Slot status must be 'open' or 'filled'")
    with _connect(db_path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS slot_status (id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL)")
        connection.execute("INSERT INTO slot_status (id, status) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status", (status,))
    return status
