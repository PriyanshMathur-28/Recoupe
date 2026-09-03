"""Small dependency-free PDF invoice generator for recovery emails."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _money(value: Any) -> float:
    try:
        amount = float(value)
        return amount if amount >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe(value: Any, fallback: str = "Not provided") -> str:
    text = str(value or "").strip()
    return text or fallback


def invoice_stage(action: str, event: dict[str, Any]) -> str:
    attempts = int(event.get("attempt_count") or 0) if str(event.get("attempt_count") or "0").isdigit() else 0
    if attempts >= 2:
        return "Final Notice"
    if action == "charge_fee":
        return "Overdue"
    return "Reminder"


def build_invoice(event: dict[str, Any], action: str, payment_link: str) -> dict[str, Any]:
    """Return invoice metadata and a valid, simple one-page PDF."""
    amount = _money(event.get("fee_amount", event.get("appointment_value", event.get("subscription_amount"))))
    partial = _money(event.get("amount_paid", event.get("partial_payment")))
    original = _money(event.get("appointment_value", event.get("subscription_amount")))
    late_fee = _money(event.get("late_fee"))
    if late_fee == 0 and action == "charge_fee" and original > amount:
        late_fee = amount - original
    subtotal = amount + late_fee
    balance = max(subtotal - partial, 0.0)
    case_id = _safe(event.get("client_id"), "case")
    digest = hashlib.sha256(f"{case_id}:{action}:{event.get('attempt_count', 0)}:{amount}:{payment_link}".encode()).hexdigest()[:8].upper()
    number = f"INV-{datetime.now(timezone.utc):%Y%m%d}-{digest}"
    due_value = event.get("due_date") or event.get("payment_due_date")
    if due_value:
        due_date = _safe(due_value)
    else:
        due_date = (date.today() + timedelta(days=7)).isoformat()
    stage = invoice_stage(action, event)
    lines = [
        "RAZORPAY RECOVERY INVOICE",
        f"{stage.upper()}  |  {number}",
        "",
        f"Bill to: {_safe(event.get('client_name'), 'Client')}",
        f"Client ID: {case_id}",
        f"Email: {_safe(event.get('client_email'))}",
        f"Billing address: {_safe(event.get('billing_address'))}",
        "",
        "DESCRIPTION                         AMOUNT (INR)",
        "-" * 58,
        f"{action.replace('_', ' ').title():35} {amount:>14,.2f}",
    ]
    if late_fee:
        lines.append(f"Late fee:                            {late_fee:>14,.2f}")
    if partial:
        lines.append(f"Previous partial payment:            {-partial:>14,.2f}")
    lines += [
        "-" * 58,
        f"BALANCE DUE:                         {balance:>14,.2f}",
        "",
        f"Due date: {due_date}",
        f"Payment link: {payment_link}",
        "",
        "Please use the payment link above to settle this balance.",
        "This invoice is generated from the recovery case audit trail.",
    ]
    pdf = _pdf(lines, stage)
    return {
        "invoice_number": number,
        "invoice_status": stage,
        "invoice_due_date": due_date,
        "invoice_amount": round(balance, 2),
        "invoice_pdf": pdf,
        "invoice_filename": f"invoice-{number}.pdf",
    }


def _pdf(lines: list[str], stage: str) -> bytes:
    """Write a minimal PDF using built-in objects; no external renderer required."""
    text = ["BT", "/F1 11 Tf", "50 760 Td"]
    for index, line in enumerate(lines):
        escaped = re.sub(r"[^\x20-\x7e]", "?", line).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            text.append("0 -18 Td")
        text.append(f"({escaped}) Tj")
    text.append("ET")
    stream = "\n".join(text).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii"))
    return bytes(output)
