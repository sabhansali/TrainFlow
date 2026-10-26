
"""TrainFlow: hash-chained audit logging."""

import hashlib
import hmac
import json
import math
import re
from pathlib import Path

from src.database import connect_db, initialize_database, utc_now


GENESIS_HASH = "GENESIS"
EVENT_TYPE_PATTERN = re.compile(r"^[A-Z0-9_.:-]{1,80}$")

SENSITIVE_KEYS = {
    "password", "password_hash", "secret", "token",
    "api_key", "authorization", "credential",
}

MAX_DETAILS_BYTES = 25_000


def _redact(value):
    if isinstance(value, dict):
        output = {}

        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")

            if any(secret in normalized for secret in SENSITIVE_KEYS):
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact(item)

        return output

    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Audit details cannot contain NaN or infinity")

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    raise ValueError(
        f"Unsupported audit detail type: {type(value).__name__}"
    )


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _event_hash(
    timestamp,
    event_type,
    actor_id,
    request_id,
    details,
    prev_hash,
):
    payload = {
        "timestamp": timestamp,
        "event_type": event_type,
        "actor_id": actor_id,
        "request_id": request_id,
        "details": details,
        "prev_hash": prev_hash,
    }

    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _append_with_connection(
    connection,
    event_type,
    actor_id,
    request_id,
    details,
):
    previous = connection.execute(
        """SELECT event_hash FROM audit_events
           ORDER BY event_id DESC LIMIT 1"""
    ).fetchone()

    previous_hash = previous["event_hash"] if previous else GENESIS_HASH
    timestamp = utc_now()

    details_json = _canonical_json(details)
    digest = _event_hash(
        timestamp,
        event_type,
        actor_id,
        request_id,
        details,
        previous_hash,
    )

    cursor = connection.execute(
        """
        INSERT INTO audit_events (
            timestamp, event_type, actor_id, request_id,
            details_json, prev_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            event_type,
            actor_id,
            request_id,
            details_json,
            previous_hash,
            digest,
        ),
    )

    return int(cursor.lastrowid)


def append_audit_event(
    db_path,
    event_type,
    actor_id=None,
    request_id=None,
    details=None,
    connection=None,
):
    """Append an audit event; reuse a connection for atomic transactions."""

    if not isinstance(event_type, str) or not EVENT_TYPE_PATTERN.fullmatch(
        event_type
    ):
        raise ValueError("Invalid event_type")

    for name, value in (
        ("actor_id", actor_id),
        ("request_id", request_id),
    ):
        if value is not None and (
            not isinstance(value, str) or len(value) > 200
        ):
            raise ValueError(f"Invalid {name}")

    if details is None:
        details = {}

    if not isinstance(details, dict):
        raise ValueError("details must be a dictionary")

    safe_details = _redact(details)
    serialized = _canonical_json(safe_details)

    if len(serialized.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("Audit details exceed the size limit")

    if connection is not None:
        return _append_with_connection(
            connection,
            event_type,
            actor_id,
            request_id,
            safe_details,
        )

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        event_id = _append_with_connection(
            connection,
            event_type,
            actor_id,
            request_id,
            safe_details,
        )
        connection.commit()

    connection.close()
    return event_id


def verify_audit_chain(db_path):
    """Verify event contents and each previous-event hash link."""

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        rows = connection.execute(
            """SELECT event_id, timestamp, event_type, actor_id,
                      request_id, details_json, prev_hash, event_hash
               FROM audit_events ORDER BY event_id"""
        ).fetchall()
    connection.close()

    previous_hash = GENESIS_HASH

    for position, row in enumerate(rows, start=1):
        try:
            details = json.loads(row["details_json"])

            if not isinstance(details, dict):
                raise ValueError("Audit details must be an object")

            if row["prev_hash"] != previous_hash:
                return {
                    "valid": False,
                    "events_checked": position - 1,
                    "first_invalid_event_id": row["event_id"],
                    "reason": "Previous-event hash link mismatch",
                }

            calculated = _event_hash(
                row["timestamp"],
                row["event_type"],
                row["actor_id"],
                row["request_id"],
                details,
                row["prev_hash"],
            )

            if not hmac.compare_digest(calculated, row["event_hash"]):
                return {
                    "valid": False,
                    "events_checked": position - 1,
                    "first_invalid_event_id": row["event_id"],
                    "reason": "Event contents do not match their hash",
                }

            previous_hash = row["event_hash"]

        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return {
                "valid": False,
                "events_checked": position - 1,
                "first_invalid_event_id": row["event_id"],
                "reason": f"Invalid event data: {exc}",
            }

    return {
        "valid": True,
        "events_checked": len(rows),
        "first_invalid_event_id": None,
        "reason": None,
        "latest_hash": previous_hash,
    }
