"""Run once after downloading a Desktop OAuth client as credentials.json."""
import os
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/gmail.send"]


def run_oauth() -> Path:
    load_dotenv()
    root = Path(__file__).resolve().parent
    credentials_path = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", str(root / "credentials.json")))
    token_path = Path(os.getenv("GOOGLE_TOKEN_FILE", str(root / "token.json")))
    if not credentials_path.is_absolute():
        credentials_path = root / credentials_path
    if not token_path.is_absolute():
        token_path = root / token_path
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return token_path


if __name__ == "__main__":
    print(f"OAuth complete. Saved {run_oauth()}")
