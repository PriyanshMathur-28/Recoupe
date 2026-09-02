"""Single-entrypoint live runner for the no-show recovery agent.

Run this file after configuring .env and completing Google OAuth. It starts the
background polling workflow, performs one immediate scan, and serves the
operations dashboard from the same Python process.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from threading import Thread
from typing import Any

from dotenv import load_dotenv

ROOT = __import__("pathlib").Path(__file__).resolve().parent


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _validate_live_configuration(include_calendar: bool) -> None:
    """Fail early with an actionable message before live side effects start."""
    required = {
        "RAZORPAY_KEY_ID": os.getenv("RAZORPAY_KEY_ID"),
        "RAZORPAY_KEY_SECRET": os.getenv("RAZORPAY_KEY_SECRET"),
        "GROQ_API_KEY or GEMINI_API_KEY": os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY"),
        "DASHBOARD_PASSWORD": os.getenv("DASHBOARD_PASSWORD"),
        "FLASK_SECRET_KEY": os.getenv("FLASK_SECRET_KEY"),
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if include_calendar:
        token_name = "GOOGLE_TOKEN_FILE"
        token_path = __import__("pathlib").Path(os.getenv(token_name, str(ROOT / "token.json")))
        if not token_path.is_absolute():
            token_path = ROOT / token_path
        if not token_path.exists():
            missing.append(f"{token_name} ({token_path.name} is missing; run oauth_flow.py once)")
    if missing:
        raise SystemExit("Missing live configuration: " + ", ".join(missing) + ". See .env.example.")


def _clear_persistent_state() -> None:
    """Clear uploaded data and logs so each run starts with a clean slate."""
    data_dir = ROOT / "data"
    if data_dir.exists():
        for item in data_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink()
                except OSError:
                    pass
    logs_dir = ROOT / "logs"
    if logs_dir.exists():
        for item in logs_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink()
                except OSError:
                    pass


def _start_dashboard(host: str, port: int) -> None:
    from dashboard import app, ensure_port_available

    ensure_port_available(host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


def run(include_calendar: bool, dashboard: bool, host: str, port: int) -> None:
    load_dotenv(ROOT / ".env")
    _configure_logging()
    _validate_live_configuration(include_calendar)

    logging.getLogger(__name__).info("Clearing previous run state for a clean slate...")
    _clear_persistent_state()

    from main import create_scheduler, process_pending_events

    # Background scanning only detects and prepares recovery cases; it never
    # delivers client emails. Passing live=False keeps the scheduler in
    # preview mode so nothing is sent automatically. Emails go out solely when
    # an operator clicks "Send email" on the dashboard, which delivers through
    # the /api/clients/.../send-email endpoints.
    logging.getLogger(__name__).info("Running initial detection scan (no automatic email delivery)")
    results = process_pending_events(include_calendar=include_calendar, live=False)
    logging.getLogger(__name__).info("Initial scan completed: %d events processed", len(results))

    scheduler = create_scheduler(include_calendar=include_calendar, live=False)
    scheduler.start()
    logging.getLogger(__name__).info("Detection polling started every 60 seconds (emails require a dashboard send)")

    if dashboard:
        _start_dashboard(host, port)
        return

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        logging.getLogger(__name__).info("Live polling stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run detection, recovery, auditing, polling, and dashboard together")
    parser.add_argument("--no-calendar", action="store_true", help="Use CSV sources only")
    parser.add_argument("--no-dashboard", action="store_true", help="Run the worker without starting Flask")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    run(
        include_calendar=not args.no_calendar,
        dashboard=not args.no_dashboard,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
