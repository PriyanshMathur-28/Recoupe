"""Validate the merged recovery case CSV and its case-type-specific constraints."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "recovery_cases.csv"
# Kept as a small compatibility map for callers that previously iterated SPECS.
SPECS = {"data/recovery_cases.csv": {}}
COMMON_COLUMNS = {"case_type", "client_id", "client_name", "client_email"}
NO_SHOW_COLUMNS = {"appointment_datetime", "appointment_value", "cancellation_time", "is_first_offense"}
SUBSCRIPTION_COLUMNS = {"subscription_amount", "failure_reason", "attempt_count", "last_charge_date"}
SUPPORTED_CASE_TYPES = {"no_show", "subscription"}
SUPPORTED_FAILURE_REASONS = {
    "card_declined",
    "card_expired",
    "insufficient_funds",
    "bank_declined",
    "payment_method_failed",
}


def _valid_positive_amount(value: object) -> bool:
    amount = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return not pd.isna(amount) and float(amount) > 0 and math.isfinite(float(amount))


def _valid_timestamp(value: object) -> bool:
    return not pd.isna(pd.to_datetime(value, errors="coerce", utc=True))


def validate_file(path: Path = CSV_PATH, spec: dict | None = None) -> list[str]:
    """Return validation errors for the unified recovery case source.

    ``spec`` is accepted for compatibility with older validation callers; the
    merged file's ``case_type`` column now determines row-specific rules.
    """
    try:
        frame = pd.read_csv(path, dtype={"client_id": "string", "case_type": "string"})
    except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        return [f"parsing error: {exc}"]

    required = COMMON_COLUMNS | NO_SHOW_COLUMNS | SUBSCRIPTION_COLUMNS
    missing = sorted(required.difference(frame.columns))
    if missing:
        return [f"missing column: {column}" for column in missing]

    errors: list[str] = []
    case_types = frame["case_type"].astype("string").str.strip()
    for index, value in case_types.items():
        if value not in SUPPORTED_CASE_TYPES:
            errors.append(f"row {index + 2}: unsupported case_type")

    identifiers = frame["client_id"].astype("string").str.strip()
    for index in frame.index[identifiers.isna() | identifiers.eq("")]:
        errors.append(f"row {index + 2}: missing client_id")
    for index in frame.index[identifiers.duplicated(keep=False)]:
        errors.append(f"row {index + 2}: duplicate client_id")
    for index, value in frame["client_email"].items():
        if pd.isna(value) or "@" not in str(value):
            errors.append(f"row {index + 2}: missing or invalid email")

    for index, row in frame.iterrows():
        row_number = index + 2
        case_type = str(row["case_type"]).strip()
        if case_type == "no_show":
            if not _valid_positive_amount(row["appointment_value"]):
                errors.append(f"row {row_number}: invalid positive appointment_value")
            appointment = pd.to_datetime(row["appointment_datetime"], errors="coerce", utc=True)
            cancellation = pd.to_datetime(row["cancellation_time"], errors="coerce", utc=True)
            if pd.isna(appointment):
                errors.append(f"row {row_number}: invalid appointment_datetime")
            if pd.isna(cancellation):
                errors.append(f"row {row_number}: invalid cancellation_time")
            if not pd.isna(appointment) and not pd.isna(cancellation) and cancellation > appointment:
                errors.append(f"row {row_number}: cancellation occurs after appointment")
            if str(row["is_first_offense"]).strip().lower() not in {"true", "false", "1", "0", "yes", "no", "y", "n"}:
                errors.append(f"row {row_number}: invalid is_first_offense")
        elif case_type == "subscription":
            if not _valid_positive_amount(row["subscription_amount"]):
                errors.append(f"row {row_number}: invalid positive subscription_amount")
            attempts = pd.to_numeric(pd.Series([row["attempt_count"]]), errors="coerce").iloc[0]
            if pd.isna(attempts) or float(attempts) < 0 or float(attempts) % 1 != 0:
                errors.append(f"row {row_number}: invalid attempt_count")
            if row["failure_reason"] not in SUPPORTED_FAILURE_REASONS:
                errors.append(f"row {row_number}: unsupported failure_reason")
            if not _valid_timestamp(row["last_charge_date"]):
                errors.append(f"row {row_number}: invalid last_charge_date")
    return errors


if __name__ == "__main__":
    validation_errors = validate_file()
    print(f"data/recovery_cases.csv: {'validation errors' if validation_errors else 'No validation errors'}")
    for error in validation_errors:
        print(f"  {error}")
    raise SystemExit(1 if validation_errors else 0)
