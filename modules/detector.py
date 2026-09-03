"""Revenue-risk event detector for synthetic CSV and live Calendar sources."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .waitlist import DB_PATH as WAITLIST_DB_PATH, has_waiting_entry

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_CASES_PATH = ROOT / "data" / "recovery_cases.csv"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
LOGGER = logging.getLogger(__name__)
SUPPORTED_SOURCES = {"no_show", "subscription", "calendar"}


def _iso(value: Any) -> str | None:
    """Return an ISO/string identifier, treating all pandas missing values as absent."""
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value).strip() or None


def _valid_datetime(value: Any) -> str | None:
    """Normalize a required datetime, returning None for missing or invalid input."""
    normalized = _iso(value)
    if normalized is None:
        return None
    parsed = pd.to_datetime(normalized, utc=True, errors="coerce")
    return None if pd.isna(parsed) else normalized


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _calendar_start(event: dict[str, Any]) -> str | None:
    start = event.get("start", event.get("appointment_datetime"))
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if not isinstance(start, (str, pd.Timestamp)):
        return None
    return _iso(start)


def _hours_between(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    parsed = pd.to_datetime([start, end], utc=True, errors="coerce")
    if parsed.isna().any():
        return None
    return float((parsed[0] - parsed[1]).total_seconds() / 3600)


def normalize_event(source: str, row: dict[str, Any]) -> dict[str, Any]:
    """Convert a source record into the common event shape and validate it."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported event source: {source}")
    client_id = row.get("client_id")
    if source == "no_show":
        identifier = _iso(client_id)
        appointment = _valid_datetime(row.get("appointment_datetime"))
        cancellation = _valid_datetime(row.get("cancellation_time"))
        errors = []
        if identifier is None:
            errors.append("missing client_id")
        if appointment is None:
            errors.append("missing or invalid appointment_datetime")
        if cancellation is None:
            errors.append("missing or invalid cancellation_time")
        return {
            "event_type": "no_show", "client_id": identifier,
            "client_name": row.get("client_name", ""), "client_email": row.get("client_email", ""),
            "appointment_datetime": appointment,
            "appointment_value": row.get("appointment_value"), "cancellation_time": cancellation,
            "urgency_hours": row.get("urgency_hours"), "urgency_policy": "invalid_or_negative_escalates",
            "is_first_offense": _bool(row.get("is_first_offense")),
            "validation_errors": errors,
            "source": "recovery_cases.csv",
        }
    if source == "subscription":
        errors = []
        try:
            numeric_attempts = float(row.get("attempt_count"))
            if not numeric_attempts.is_integer() or numeric_attempts < 0:
                raise ValueError
            attempts = int(numeric_attempts)
        except (TypeError, ValueError):
            attempts = None
            errors.append("invalid attempt_count")
        amount = pd.to_numeric(pd.Series([row.get("subscription_amount")]), errors="coerce").iloc[0]
        if pd.isna(amount) or float(amount) <= 0:
            amount = None
            errors.append("invalid subscription_amount")
        email = row.get("client_email")
        try:
            missing_email = bool(pd.isna(email))
        except (TypeError, ValueError):
            missing_email = False
        if missing_email or "@" not in str(email):
            email = None
            errors.append("missing or invalid client_email")
        identifier = _iso(client_id)
        if identifier is None:
            errors.append("missing client_id")
        return {
            "event_type": "failed_subscription", "client_id": identifier,
            "client_name": row.get("client_name", ""), "client_email": email,
            "subscription_amount": amount, "failure_reason": row.get("failure_reason", ""),
            "attempt_count": attempts, "last_charge_date": _iso(row.get("last_charge_date")),
            "validation_errors": errors, "source": "recovery_cases.csv",
        }
    raw_start = row.get("start", row.get("appointment_datetime"))
    start = _calendar_start(row)
    if raw_start is not None and start is None:
        raise ValueError("missing or invalid calendar start")
    cancellation = _iso(row.get("updated", row.get("cancellation_time")))
    calendar_id = row.get("id", client_id)
    identifier = _iso(calendar_id)
    return {
        "event_type": "calendar_cancellation", "client_id": identifier,
        "client_name": row.get("summary", row.get("client_name", "")), "client_email": row.get("client_email", ""),
        "appointment_datetime": start, "cancellation_time": cancellation,
        "urgency_hours": _hours_between(start, cancellation), "urgency_policy": "invalid_or_negative_escalates",
        "is_first_offense": _bool(row.get("is_first_offense", False)),
        "validation_errors": ["missing client_id"] if identifier is None else [],
        "source": "google_calendar",
    }


def _source_error_event(source: str, error: Exception, row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Represent a source failure as an auditable event instead of aborting a batch."""
    row = row or {}
    return {
        "event_type": "source_error",
        "client_id": _iso(row.get("client_id")) or "unknown",
        "client_name": row.get("client_name", ""),
        "validation_errors": [f"{source} parsing failed: {error}"],
        "source": source,
    }


def check_no_shows(csv_path: Path | None = None) -> list[dict[str, Any]]:
    path = csv_path or RECOVERY_CASES_PATH
    try:
        frame = pd.read_csv(path)
        if "case_type" in frame.columns:
            frame = frame[frame["case_type"] == "no_show"].copy()
        required = {"appointment_datetime", "cancellation_time"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Missing no-show columns: {', '.join(sorted(missing))}")
        appointment = pd.to_datetime(frame["appointment_datetime"], utc=True, errors="coerce")
        cancellation = pd.to_datetime(frame["cancellation_time"], utc=True, errors="coerce")
        if appointment.isna().any() or cancellation.isna().any():
            raise ValueError(f"Invalid no-show datetime in {path}")
        frame["appointment_datetime"], frame["cancellation_time"] = appointment, cancellation
        frame["urgency_hours"] = (appointment - cancellation).dt.total_seconds() / 3600
    except Exception as exc:
        return [_source_error_event(path.name, exc)]
    events = []
    for row in frame.to_dict(orient="records"):
        try:
            events.append(normalize_event("no_show", row))
        except Exception as exc:
            events.append(_source_error_event(path.name, exc, row))
    return events


def check_calendar_live(service: Any = None, now: Any = None) -> list[dict[str, Any]]:
    """Read cancelled primary-calendar events in a bounded window."""
    try:
        if service is None:
            token = Path(os.getenv("GOOGLE_TOKEN_FILE", str(ROOT / "token.json")))
            if not token.is_absolute():
                token = ROOT / token
            if not token.exists():
                return []
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            credentials = Credentials.from_authorized_user_file(str(token), [CALENDAR_SCOPE])
            service = build("calendar", "v3", credentials=credentials)
        now_value = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz="UTC"))
        time_min = (now_value - pd.Timedelta(days=1)).isoformat()
        time_max = (now_value + pd.Timedelta(days=1)).isoformat()
        events: list[dict[str, Any]] = []
        page_token = None
        while True:
            request = service.events().list(calendarId="primary", showDeleted=True, singleEvents=True, orderBy="updated", timeMin=time_min, timeMax=time_max, pageToken=page_token)
            response = request.execute()
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        normalized = []
        for event in events:
            if event.get("status") != "cancelled":
                continue
            try:
                normalized.append(normalize_event("calendar", event))
            except Exception as exc:
                normalized.append(_source_error_event("google_calendar", exc, event))
        return normalized
    except Exception as exc:
        LOGGER.warning("Calendar source unavailable: %s", exc)
        return []


def check_failed_subscriptions(csv_path: Path | None = None) -> list[dict[str, Any]]:
    path = csv_path or RECOVERY_CASES_PATH
    try:
        frame = pd.read_csv(path)
        if "case_type" in frame.columns:
            frame = frame[frame["case_type"] == "subscription"].copy()
    except Exception as exc:
        return [_source_error_event(path.name, exc)]
    events = []
    for row in frame.to_dict(orient="records"):
        try:
            events.append(normalize_event("subscription", row))
        except Exception as exc:
            events.append(_source_error_event(path.name, exc, row))
    return events


def get_all_risk_events(include_calendar: bool = True, waitlist_path: Path = WAITLIST_DB_PATH) -> list[dict[str, Any]]:
    events = check_no_shows() + check_failed_subscriptions()
    if include_calendar:
        events.extend(check_calendar_live())
    waiting = has_waiting_entry(waitlist_path)
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, original in enumerate(events):
        event = dict(original)
        if event.get("event_type") in {"no_show", "calendar_cancellation"}:
            event["waitlist_entry_exists"] = waiting
        key = (event.get("source"), event.get("event_type"), event.get("client_id"), event.get("appointment_datetime"), event.get("last_charge_date"))
        if not str(event.get("client_id") or "").strip():
            key = (*key, index)
        unique.setdefault(key, event)
    return list(unique.values())


if __name__ == "__main__":
    events = get_all_risk_events()
    print(f"Detected {len(events)} risk events")
    for event in events:
        print(event)
