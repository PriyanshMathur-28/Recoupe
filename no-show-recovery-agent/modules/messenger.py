"""Send Gmail messages using the Phase 1 OAuth token."""
from __future__ import annotations

import base64
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DEFAULT_GMAIL_HTTP_TIMEOUT_SECONDS = 30.0


def _gmail_timeout_seconds() -> float:
    """Return the positive socket timeout configured for Gmail API calls."""
    raw_value = os.getenv("GMAIL_HTTP_TIMEOUT_SECONDS", str(DEFAULT_GMAIL_HTTP_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("GMAIL_HTTP_TIMEOUT_SECONDS must be a positive number") from exc
    if timeout <= 0:
        raise RuntimeError("GMAIL_HTTP_TIMEOUT_SECONDS must be a positive number")
    return timeout


def _gmail_service(service: Any = None) -> Any:
    if service is not None:
        return service
    load_dotenv(ROOT / ".env")
    import httplib2
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    token_path = Path(os.getenv("GOOGLE_TOKEN_FILE", str(ROOT / "token.json")))
    if not token_path.is_absolute():
        token_path = ROOT / token_path
    if not token_path.exists():
        raise RuntimeError("token.json is required for Gmail delivery")
    credentials = Credentials.from_authorized_user_file(str(token_path), [GMAIL_SCOPE])
    transport = AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=_gmail_timeout_seconds()),
    )
    return build("gmail", "v1", http=transport, cache_discovery=False)


def send_email(to_email: str, subject: str, body: str, service: Any = None, attachment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a UTF-8 email through Gmail API, optionally with one PDF attachment."""
    if "@" not in str(to_email or ""):
        raise ValueError("A valid recipient email is required")
    if attachment:
        message = MIMEMultipart("mixed")
        message.attach(MIMEText(body, "plain", "utf-8"))
        part = MIMEApplication(attachment["content"], _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attachment["filename"])
        message.attach(part)
    else:
        message = MIMEText(body, "plain", "utf-8")
    message["to"], message["subject"] = to_email, subject
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return _gmail_service(service).users().messages().send(userId="me", body={"raw": encoded}).execute()


def send_message(to_email: str, subject: str, body: str, payment_link: str | None = None, service: Any = None, attachment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a recovery message, appending its payment link and optional bill."""
    if payment_link:
        body = f"{body.rstrip()}\n\nPayment link: {payment_link}"
    if attachment:
        body = f"{body.rstrip()}\n\nInvoice attached: {attachment['filename']}"
    return send_email(to_email, subject, body, service=service, attachment=attachment)
