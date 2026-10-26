
"""TrainFlow: input loading, normalization, validation, and quarantine."""

import csv
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any


FIELDS = [
    "request_id",
    "employee_id",
    "employee_name",
    "email",
    "employee_role",
    "training_course",
    "role_relevance_score",
    "skill_gap_alignment_score",
    "prerequisite_completion_score",
    "manager_id",
    "request_date",
    "justification",
]

SCORE_FIELDS = [
    "role_relevance_score",
    "skill_gap_alignment_score",
    "prerequisite_completion_score",
]

TEXT_FIELDS = [
    "request_id",
    "employee_id",
    "employee_name",
    "email",
    "employee_role",
    "training_course",
    "manager_id",
    "request_date",
    "justification",
]

REQUIRED_FIELDS = FIELDS.copy()

DEFAULT_ROLES = ["Software Engineer", "Data Analyst"]

EMAIL_PATTERN = re.compile(
    r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
)

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_RECORDS = 10_000
MAX_FIELD_LENGTH = 2_000


def normalize_record(record: dict[str, Any]) -> tuple[dict, list[str]]:
    """Normalize a record without interpreting employee text as instructions."""

    normalized = {}

    for field in FIELDS:
        value = record.get(field, "")

        if value is None:
            value = ""
        elif isinstance(value, str):
            value = value.strip()

        normalized[field] = value

    # Canonicalize identifiers to reduce case-based duplicate bypasses.
    if isinstance(normalized["request_id"], str):
        normalized["request_id"] = normalized["request_id"].upper()

    if isinstance(normalized["employee_id"], str):
        normalized["employee_id"] = normalized["employee_id"].upper()

    if isinstance(normalized["manager_id"], str):
        normalized["manager_id"] = normalized["manager_id"].upper()

    if isinstance(normalized["email"], str):
        normalized["email"] = normalized["email"].lower()

    unexpected_fields = sorted(set(record) - set(FIELDS))

    return normalized, unexpected_fields


def _validate_single_record(
    record: dict[str, Any],
    allowed_roles: list[str],
) -> tuple[dict, list[str]]:
    """Return the normalized record and all validation errors."""

    normalized, unexpected_fields = normalize_record(record)
    errors = []

    if unexpected_fields:
        errors.append(
            "Unexpected fields: " + ", ".join(unexpected_fields)
        )

    # Check field types, required values, lengths, and control characters.
    for field in TEXT_FIELDS:
        value = normalized[field]

        if not isinstance(value, str):
            errors.append(f"{field} must be text")
            continue

        if not value:
            errors.append(f"Missing required field: {field}")
            continue

        if len(value) > MAX_FIELD_LENGTH:
            errors.append(f"{field} exceeds the maximum length")

        # Preserve ordinary newlines and tabs, but reject unsafe control bytes.
        if any(
            ord(char) < 32 and char not in "\n\r\t"
            for char in value
        ):
            errors.append(f"{field} contains invalid control characters")

    # Email validation.
    email = normalized["email"]
    if isinstance(email, str) and email:
        if not EMAIL_PATTERN.fullmatch(email):
            errors.append("Invalid email format")

    # Employee role must match one of the configured roles.
    role = normalized["employee_role"]
    role_lookup = {
        allowed_role.casefold(): allowed_role
        for allowed_role in allowed_roles
    }

    if isinstance(role, str) and role:
        canonical_role = role_lookup.get(role.casefold())

        if canonical_role is None:
            errors.append("Unsupported employee_role")
        else:
            normalized["employee_role"] = canonical_role

    # Parse scores defensively and reject NaN, infinity, and out-of-range values.
    for field in SCORE_FIELDS:
        value = normalized[field]

        if isinstance(value, bool):
            errors.append(f"{field} must be a numeric score")
            continue

        try:
            score = float(value)
        except (TypeError, ValueError):
            errors.append(f"{field} must be a numeric score")
            continue

        if not math.isfinite(score):
            errors.append(f"{field} must be finite")
            continue

        if not 0 <= score <= 100:
            errors.append(f"{field} must be between 0 and 100")
            continue

        normalized[field] = score

    # Require a real ISO-formatted calendar date.
    request_date = normalized["request_date"]

    if isinstance(request_date, str) and request_date:
        try:
            date.fromisoformat(request_date)
        except ValueError:
            errors.append("request_date must be a valid YYYY-MM-DD date")

    # Employee-provided justifications remain untrusted data.
    # Do not execute them, treat them as instructions, or use them for scoring.

    return normalized, errors


def validate_records(
    records: list[dict[str, Any]],
    allowed_roles: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Validate a batch.

    Returns:
        (valid_records, rejected_records)

    The first occurrence of a request ID reserves that ID. Subsequent
    occurrences are quarantined, even if the first occurrence is invalid.
    """

    if not isinstance(records, list):
        raise TypeError("records must be a list")

    if len(records) > MAX_RECORDS:
        raise ValueError(f"Maximum batch size is {MAX_RECORDS} records")

    roles = allowed_roles or DEFAULT_ROLES

    if (
        not isinstance(roles, list)
        or not roles
        or any(not isinstance(role, str) or not role.strip()
               for role in roles)
    ):
        raise ValueError("allowed_roles must be a non-empty list of strings")

    valid_records = []
    rejected_records = []
    seen_request_ids = set()

    for row_number, original in enumerate(records, start=2):
        if not isinstance(original, dict):
            rejected_records.append({
                "source_row": row_number,
                "rejection_reasons": "Record must be a JSON object",
            })
            continue

        normalized, errors = _validate_single_record(original, roles)

        request_id = normalized.get("request_id", "")

        if isinstance(request_id, str) and request_id:
            duplicate_key = request_id.casefold()

            if duplicate_key in seen_request_ids:
                errors.append("Duplicate request_id")
            else:
                seen_request_ids.add(duplicate_key)

        if errors:
            rejected_records.append({
                **normalized,
                "source_row": row_number,
                "rejection_reasons": "; ".join(errors),
            })
        else:
            valid_records.append(normalized)

    return valid_records, rejected_records


def load_records(file_path: str | Path) -> list[dict]:
    """Load a CSV or JSON batch with file, schema, and size checks."""

    path = Path(file_path)

    if path.suffix.lower() not in {".csv", ".json"}:
        raise ValueError("Only CSV and JSON files are supported")

    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Input file exceeds the 10 MB limit")

    if path.suffix.lower() == ".csv":
        try:
            with path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as file:
                reader = csv.DictReader(file)

                if reader.fieldnames is None:
                    raise ValueError("CSV file has no header row")

                headers = reader.fieldnames

                if len(headers) != len(set(headers)):
                    raise ValueError("CSV contains duplicate column headers")

                missing = set(FIELDS) - set(headers)
                unexpected = set(headers) - set(FIELDS)

                if missing:
                    raise ValueError(
                        "CSV is missing required headers: "
                        + ", ".join(sorted(missing))
                    )

                if unexpected:
                    raise ValueError(
                        "CSV contains unexpected headers: "
                        + ", ".join(sorted(unexpected))
                    )

                records = list(reader)

        except csv.Error as exc:
            raise ValueError(f"Malformed CSV: {exc}") from exc

    else:
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed JSON: {exc}") from exc

        if isinstance(data, dict):
            data = data.get("requests", data.get("records"))

        if not isinstance(data, list):
            raise ValueError(
                "JSON must contain a list of records or a "
                "'requests'/'records' list"
            )

        records = data

        if any(not isinstance(record, dict) for record in records):
            raise ValueError("Each JSON record must be an object")

    if len(records) > MAX_RECORDS:
        raise ValueError(f"Maximum batch size is {MAX_RECORDS} records")

    return records
