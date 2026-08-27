"""Throwaway end-to-end smoke check for the clients feature."""
import json
import tempfile
from pathlib import Path

from modules.audit_log import log_event
from modules.service_layer import RecoveryService

tmp = Path(tempfile.mkdtemp())
audit, attempts, waitlist = tmp / "a.csv", tmp / "b.sqlite3", tmp / "c.sqlite3"

event = {"event_type": "no_show", "client_id": "C1", "client_name": "Asha Rao",
         "client_email": "asha@example.com", "urgency_hours": 1.0,
         "fee_amount": 500, "is_first_offense": False, "source": "csv"}
log_event(event, "charge_fee", "msg", "link_created", audit, outcome="action_completed")

svc = RecoveryService(audit, attempts, waitlist)
clients = svc.list_clients()
print("STEP 3 list_clients:", json.dumps(clients, indent=2, default=str)[:600])

sent_calls = []


class FakeGmail:
    def users(self): return self
    def messages(self): return self
    def send(self, userId, body): sent_calls.append(body); return self
    def execute(self): return {"id": "gmail-1"}


class FakePay:
    def __init__(self): self.payment_link = self
    def create(self, payload): return {"id": "plink_1", "short_url": "https://rzp.io/x"}


try:
    result = svc.send_client_email("C1", payment_client=FakePay(), message_service=FakeGmail(),
                                   llm_call=lambda p: "Hello, please settle the fee.")
    print("\nSTEP 4 send-email OK. gmail calls:", len(sent_calls),
          "| sent_at:", result["last_email_sent_at"])
except Exception as exc:
    print("\nSTEP 4 FAILED:", type(exc).__name__, exc)

after = svc.list_clients()
print("STEP 4 status after send: email_sent =", after[0]["email_sent"])

try:
    svc.send_client_email("C1", payment_client=FakePay(), message_service=FakeGmail())
    print("BUG: duplicate send allowed")
except ValueError as exc:
    print("Duplicate blocked:", exc)

# New case for same client -> button must re-enable
log_event({**event, "urgency_hours": 0.5, "fee_amount": 900}, "charge_fee", "m2",
          "link_created", audit, outcome="action_completed")
print("STEP 7 re-enable on new case: email_sent =", RecoveryService(audit, attempts, waitlist).list_clients()[0]["email_sent"])
