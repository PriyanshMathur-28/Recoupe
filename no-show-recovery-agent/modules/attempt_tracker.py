"""Persistent per-client stopping-rule and escalation tracking.

Stopping rules enforced here (all **self-imposed operating policy**):
  • MAX_ATTEMPTS = 3   — absolute cap per client per action scope
  • COOLDOWN_HOURS = 24 — minimum gap between consecutive payment retries
  • Contact-window guard — no outreach sent between 22:00 and 08:00 IST

Compliance framing
------------------
These caps are our own product policy. They borrow the *spirit* of RBI's
fair-practice principles on contact windows and non-harassment, but this
project collects commercial B2B receivables and subscription/checkout
failures — a different regulatory regime from consumer loan recovery by
Regulated Entities. **No RBI compliance is claimed here.**

The compliance surfaces that actually apply to this system are:
  • TRAI DLT registration — required for bulk commercial SMS/WhatsApp in
    India. Email is the only channel wired end-to-end for exactly this
    reason; adding SMS/WhatsApp requires a registered sender + template.
  • DPDP Act 2023 — consent and purpose limitation for customer PII. Only
    the minimum contact field needed for the outreach is read, and the LLM
    diagnosis layer never receives PII at all (see modules/diagnosis.py).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "attempts.sqlite3"
MAX_ATTEMPTS = 3
COOLDOWN_HOURS = 24
_IST = ZoneInfo("Asia/Kolkata")
# Hours (IST) during which automated outreach is prohibited.
_QUIET_START = 22  # 10 PM
_QUIET_END = 8    # 8 AM


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    columns = {row[1] for row in connection.execute("PRAGMA table_info(client_attempts)")}
    if columns and "action_scope" not in columns:
        connection.execute("ALTER TABLE client_attempts RENAME TO client_attempts_legacy")
    connection.execute("CREATE TABLE IF NOT EXISTS client_attempts (client_id TEXT NOT NULL, action_scope TEXT NOT NULL DEFAULT 'payment', attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, last_attempt_at TEXT, PRIMARY KEY (client_id, action_scope))")
    # Migrate: add last_attempt_at to existing tables.
    attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(client_attempts)")}
    if "last_attempt_at" not in attempt_columns and "client_id" in attempt_columns:
        try:
            connection.execute("ALTER TABLE client_attempts ADD COLUMN last_attempt_at TEXT")
        except Exception:
            pass
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
            "INSERT INTO client_attempts (client_id, action_scope, attempt_count, updated_at, last_attempt_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_id, action_scope) DO UPDATE SET attempt_count = MAX(attempt_count, ?) + 1, updated_at = excluded.updated_at, last_attempt_at = excluded.last_attempt_at",
            (str(client_id), action_scope, max(0, baseline) + 1, now, now, max(0, baseline)),
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


# ---------------------------------------------------------------------------
# Stopping-rule helpers — self-imposed contact policy (see module docstring)
# ---------------------------------------------------------------------------

def check_cooldown(client_id: str, db_path: Path = DB_PATH, action_scope: str = "payment") -> bool:
    """Return True if the last payment attempt is within the COOLDOWN_HOURS window."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT last_attempt_at FROM client_attempts WHERE client_id = ? AND action_scope = ?",
            (str(client_id), action_scope),
        ).fetchone()
    if not row or not row["last_attempt_at"]:
        return False
    try:
        last = datetime.fromisoformat(row["last_attempt_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return age_hours < COOLDOWN_HOURS


def get_next_retry_at(client_id: str, db_path: Path = DB_PATH, action_scope: str = "payment") -> str | None:
    """Return the ISO timestamp when the cooldown window lifts, or None if no cooldown."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT last_attempt_at FROM client_attempts WHERE client_id = ? AND action_scope = ?",
            (str(client_id), action_scope),
        ).fetchone()
    if not row or not row["last_attempt_at"]:
        return None
    try:
        last = datetime.fromisoformat(row["last_attempt_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (last + timedelta(hours=COOLDOWN_HOURS)).isoformat()


def is_contact_hour_allowed(now: datetime | None = None) -> bool:
    """Return False if the current IST hour falls in the quiet window (22:00–08:00)."""
    check = (now or datetime.now(timezone.utc)).astimezone(_IST)
    hour = check.hour
    return not (hour >= _QUIET_START or hour < _QUIET_END)


def check_stopping_rules(
    client_id: str,
    attempt_count: int,
    db_path: Path = DB_PATH,
    action_scope: str = "payment",
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """Return ``(reason_code, reason)`` for the first rule that blocks, else None.

    Rules checked (in order), all self-imposed operating policy:
    1. ``attempt_limit``          — absolute cap (MAX_ATTEMPTS = 3).
    2. ``cooldown_active``        — 24-hour gap between consecutive retries.
    3. ``outside_contact_window`` — no automated outreach 22:00–08:00 IST.

    The reason_code matters more than the prose: the policy engine maps
    ``attempt_limit`` to a human escalation, while ``cooldown_active`` and
    ``outside_contact_window`` are *deferrals* that stay in the automated
    queue. Collapsing all three into one "blocked" state is what produced
    the 88% escalation rate in the first batch run.
    """
    if attempt_count >= MAX_ATTEMPTS:
        return (
            "attempt_limit",
            f"No response after {attempt_count} of {MAX_ATTEMPTS} automated attempts — escalated to human review",
        )
    if check_cooldown(client_id, db_path, action_scope):
        next_at = get_next_retry_at(client_id, db_path, action_scope) or ""
        return (
            "cooldown_active",
            f"{COOLDOWN_HOURS}-hour retry cooldown active — next retry window opens at {next_at[:16].replace('T', ' ')} UTC",
        )
    if not is_contact_hour_allowed(now):
        return (
            "outside_contact_window",
            f"Outside self-imposed contact window ({_QUIET_END}:00–{_QUIET_START}:00 IST) — outreach held until {_QUIET_END}:00 IST",
        )
    return None
