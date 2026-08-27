"""Scheduled orchestration entrypoint for the recovery agent."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from modules.audit_log import AUDIT_PATH
from modules.attempt_tracker import DB_PATH as ATTEMPTS_DB_PATH
from modules.detector import get_all_risk_events
from modules.service_layer import RecoveryService
from modules.waitlist import DB_PATH as WAITLIST_DB_PATH

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / "data" / "agent_state.sqlite3"
SCHEDULE_SECONDS = 60


def _event_key(event: dict[str, Any]) -> str:
    """Create a stable identity from all meaningful event fields."""
    payload = json.dumps(event, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_event(event: dict[str, Any], store_path: Path = STORE_PATH, audit_path: Path = AUDIT_PATH, attempts_path: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    """Atomically claim and process one event exactly once across workers."""
    key = _event_key(event)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_attempts_path = attempts_path or ATTEMPTS_DB_PATH
    waitlist_path = kwargs.pop("waitlist_path", WAITLIST_DB_PATH)
    service = RecoveryService(audit_path, resolved_attempts_path, waitlist_path)
    event_json = json.dumps(event, default=str, sort_keys=True)
    with sqlite3.connect(store_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("CREATE TABLE IF NOT EXISTS processed_events (event_key TEXT PRIMARY KEY, event_json TEXT NOT NULL, action TEXT NOT NULL, processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO processed_events (event_key, event_json, action) VALUES (?, ?, ?)",
            (key, event_json, "processing"),
        )
        connection.commit()
        if cursor.rowcount != 1:
            return {"event": event, "skipped": True, "reason": "already_processed"}
    try:
        result = service.process_event(event, **kwargs)
    except Exception:
        with sqlite3.connect(store_path, timeout=30) as connection:
            connection.execute("DELETE FROM processed_events WHERE event_key = ? AND action = 'processing'", (key,))
            connection.commit()
        raise
    with sqlite3.connect(store_path, timeout=30) as connection:
        connection.execute("UPDATE processed_events SET action = ? WHERE event_key = ?", (result["action"], key))
        connection.commit()
    return result


def process_pending_events(include_calendar: bool = True, event_loader: Callable[[], list[dict[str, Any]]] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """Fetch all current risk events and process each previously unseen event."""
    events = event_loader() if event_loader is not None else get_all_risk_events(include_calendar=include_calendar)
    return [process_event(event, **kwargs) for event in events]


def create_scheduler(include_calendar: bool = True, **kwargs: Any) -> BackgroundScheduler:
    """Create a scheduler configured to poll risk sources every 60 seconds."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(process_pending_events, "interval", seconds=SCHEDULE_SECONDS, kwargs={"include_calendar": include_calendar, **kwargs}, id="risk-event-poll", replace_existing=True, max_instances=1, coalesce=True)
    return scheduler


def main() -> None:
    scheduler = create_scheduler(include_calendar=True)
    scheduler.start()
    try:
        process_pending_events(include_calendar=True)
        print(f"Scheduler running; polling every {SCHEDULE_SECONDS} seconds")
        import time
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
