
"""TrainFlow: SQLite persistence and transactional state updates."""

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "trainflow.db"

FINAL_STATUSES = {
    "PENDING_REVIEW",
    "REQUIRES_REVIEW",
    "ON_HOLD",
    "AWAITING_INFORMATION",
    "APPROVED",
}

PROPOSED_STATUSES = {
    "MANAGER_APPROVAL_REVIEW",
    "LND_REVIEW",
    "NEEDS_JUSTIFICATION_REVIEW",
}

NOTIFICATION_STATUSES = {
    "DRAFT", "APPROVED", "SENT", "FAILED", "NOT_SENT"
}

REVIEW_ACTIONS = {
    "APPROVE", "EDIT", "HOLD", "REQUEST_INFO", "REOPEN"
}

REQUEST_FIELDS = {
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
    "score",
    "score_breakdown",
    "rule_version",
    "proposed_status",
    "revised_status",
    "final_status",
    "reviewer_action",
    "reviewer_id",
    "reviewer_role",
    "review_notes",
    "notification_status",
}


class DuplicateImportError(Exception):
    """Raised when an import batch ID is reused."""


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def connect_db(db_path=None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        str(path),
        timeout=10,
        isolation_level="DEFERRED",
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")

    return connection


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _ensure_column(connection, table, column, declaration):
    columns = {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }
    if column not in columns:
        # table and declaration are constants supplied by this module.
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def initialize_database(db_path=None) -> Path:
    path = Path(db_path or DEFAULT_DB_PATH)

    schema = """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (
            role IN (
                'ADMIN', 'MANAGER', 'LND_REVIEWER',
                'EMPLOYEE', 'READ_ONLY'
            )
        ),
        is_active INTEGER NOT NULL DEFAULT 1
            CHECK (is_active IN (0, 1)),
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );

    CREATE TABLE IF NOT EXISTS training_requests (
        request_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        employee_name TEXT NOT NULL,
        email TEXT NOT NULL,
        employee_role TEXT NOT NULL,
        training_course TEXT NOT NULL,
        role_relevance_score REAL NOT NULL
            CHECK (role_relevance_score BETWEEN 0 AND 100),
        skill_gap_alignment_score REAL NOT NULL
            CHECK (skill_gap_alignment_score BETWEEN 0 AND 100),
        prerequisite_completion_score REAL NOT NULL
            CHECK (prerequisite_completion_score BETWEEN 0 AND 100),
        manager_id TEXT NOT NULL,
        request_date TEXT NOT NULL,
        justification TEXT NOT NULL,
        score REAL NOT NULL CHECK (score BETWEEN 0 AND 100),
        score_breakdown_json TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        proposed_status TEXT NOT NULL,
        revised_status TEXT,
        final_status TEXT NOT NULL,
        reviewer_action TEXT,
        reviewer_id TEXT,
        reviewer_role TEXT,
        review_notes TEXT,
        notification_status TEXT NOT NULL DEFAULT 'DRAFT',
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_requests_employee
        ON training_requests(employee_id);
    CREATE INDEX IF NOT EXISTS idx_requests_proposed_status
        ON training_requests(proposed_status);
    CREATE INDEX IF NOT EXISTS idx_requests_final_status
        ON training_requests(final_status);

    CREATE TABLE IF NOT EXISTS audit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        actor_id TEXT,
        request_id TEXT,
        details_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_audit_request
        ON audit_events(request_id);

    CREATE TABLE IF NOT EXISTS import_batches (
        batch_id TEXT PRIMARY KEY,
        source_name TEXT NOT NULL,
        total_records INTEGER NOT NULL,
        inserted_records INTEGER NOT NULL DEFAULT 0,
        skipped_records INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        source_sha256 TEXT,
        valid_records INTEGER NOT NULL DEFAULT 0,
        rejected_records INTEGER NOT NULL DEFAULT 0,
        actor_id TEXT
    );

    CREATE TABLE IF NOT EXISTS rejected_records (
        rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL
            REFERENCES import_batches(batch_id),
        source_row INTEGER NOT NULL,
        request_id TEXT,
        reason TEXT NOT NULL,
        record_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_rejected_batch
        ON rejected_records(batch_id);

    CREATE TABLE IF NOT EXISTS notification_drafts (
        notification_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL
            REFERENCES training_requests(request_id),
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('DRAFT', 'APPROVED', 'SENT', 'FAILED', 'NOT_SENT')
        ),
        created_by TEXT NOT NULL,
        approved_by TEXT,
        approved_role TEXT,
        approved_at TEXT,
        approved_content_hash TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
            CHECK (attempts BETWEEN 0 AND 3),
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_notification_request
        ON notification_drafts(request_id);

    CREATE TABLE IF NOT EXISTS notification_attempts (
        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_id TEXT NOT NULL
            REFERENCES notification_drafts(notification_id),
        attempt_number INTEGER NOT NULL
            CHECK (attempt_number BETWEEN 1 AND 3),
        actor_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN ('SENT', 'FAILED')),
        error TEXT,
        UNIQUE(notification_id, attempt_number)
    );
    """

    with closing(connect_db(path)) as connection:
        connection.executescript(schema)

        # Migrate databases that were created with the earlier lab schema.
        _ensure_column(connection, "import_batches", "source_sha256", "TEXT")
        _ensure_column(
            connection, "import_batches",
            "valid_records", "INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_column(
            connection, "import_batches",
            "rejected_records", "INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_column(connection, "import_batches", "actor_id", "TEXT")

        connection.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_import_source_hash
        ON import_batches(source_sha256)
        WHERE source_sha256 IS NOT NULL;

        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'Audit events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'Audit events are append-only');
        END;
        """)

    return path


def _request_values(request: dict) -> tuple:
    missing = REQUEST_FIELDS - set(request)
    if missing:
        raise ValueError(
            "Request is missing fields: " + ", ".join(sorted(missing))
        )

    request_id = request["request_id"]
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id is required")

    for field in (
        "role_relevance_score",
        "skill_gap_alignment_score",
        "prerequisite_completion_score",
        "score",
    ):
        value = request[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be numeric")
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{field} must be finite and between 0 and 100")

    if request["final_status"] not in FINAL_STATUSES:
        raise ValueError("Invalid final_status")

    if request["proposed_status"] not in PROPOSED_STATUSES:
        raise ValueError("Invalid proposed_status")

    if (
        request["revised_status"] is not None
        and request["revised_status"] not in PROPOSED_STATUSES
    ):
        raise ValueError("Invalid revised_status")

    if request["notification_status"] not in NOTIFICATION_STATUSES:
        raise ValueError("Invalid notification_status")

    action = request["reviewer_action"]
    if action is not None and action not in REVIEW_ACTIONS:
        raise ValueError("Invalid reviewer_action")

    now = utc_now()

    return (
        request["request_id"],
        request["employee_id"],
        request["employee_name"],
        request["email"],
        request["employee_role"],
        request["training_course"],
        float(request["role_relevance_score"]),
        float(request["skill_gap_alignment_score"]),
        float(request["prerequisite_completion_score"]),
        request["manager_id"],
        request["request_date"],
        request["justification"],
        float(request["score"]),
        _json(request["score_breakdown"]),
        request["rule_version"],
        request["proposed_status"],
        request["revised_status"],
        request["final_status"],
        request["reviewer_action"],
        request["reviewer_id"],
        request["reviewer_role"],
        request["review_notes"],
        request["notification_status"],
        _json(request),
        request.get("created_at", now),
        request.get("updated_at", now),
    )


INSERT_REQUEST_SQL = """
INSERT OR IGNORE INTO training_requests (
    request_id, employee_id, employee_name, email,
    employee_role, training_course,
    role_relevance_score, skill_gap_alignment_score,
    prerequisite_completion_score, manager_id, request_date,
    justification, score, score_breakdown_json, rule_version,
    proposed_status, revised_status, final_status, reviewer_action,
    reviewer_id, reviewer_role, review_notes, notification_status,
    payload_json, created_at, updated_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?
)
"""


def _insert_request(connection, request) -> bool:
    values = _request_values(request)
    cursor = connection.execute(INSERT_REQUEST_SQL, values)
    return cursor.rowcount == 1


def _row_to_request(row) -> dict:
    request = json.loads(row["payload_json"])

    for key in (
        "request_id", "employee_id", "employee_name", "email",
        "employee_role", "training_course", "role_relevance_score",
        "skill_gap_alignment_score", "prerequisite_completion_score",
        "manager_id", "request_date", "justification", "score",
        "rule_version", "proposed_status", "revised_status",
        "final_status", "reviewer_action", "reviewer_id",
        "reviewer_role", "review_notes", "notification_status",
        "created_at", "updated_at",
    ):
        request[key] = row[key]

    request["score_breakdown"] = json.loads(
        row["score_breakdown_json"]
    )
    return request


def get_request(db_path, request_id):
    path = initialize_database(db_path)

    with closing(connect_db(path)) as connection:
        row = connection.execute(
            "SELECT * FROM training_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    return _row_to_request(row) if row else None


def list_requests(db_path, final_status=None, limit=1000):
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")

    path = initialize_database(db_path)

    with closing(connect_db(path)) as connection:
        if final_status is None:
            rows = connection.execute(
                "SELECT * FROM training_requests ORDER BY request_id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM training_requests
                   WHERE final_status = ?
                   ORDER BY request_id LIMIT ?""",
                (final_status, limit),
            ).fetchall()

    return [_row_to_request(row) for row in rows]


def save_processed_requests(db_path, requests):
    """Compatibility helper for test fixtures; production imports use batches."""

    path = initialize_database(db_path)
    inserted = 0
    skipped_ids = []

    with closing(connect_db(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")

        for request in requests:
            if _insert_request(connection, request):
                inserted += 1
            else:
                skipped_ids.append(request["request_id"])

    return {
        "inserted": inserted,
        "skipped": len(skipped_ids),
        "skipped_request_ids": skipped_ids,
    }


def persist_request_update(
    connection,
    request: dict,
    expected_previous_status: str,
) -> None:
    """Update a request conditionally inside the caller's transaction."""

    values = _request_values(request)

    cursor = connection.execute(
        """
        UPDATE training_requests
        SET employee_id = ?,
            employee_name = ?,
            email = ?,
            employee_role = ?,
            training_course = ?,
            role_relevance_score = ?,
            skill_gap_alignment_score = ?,
            prerequisite_completion_score = ?,
            manager_id = ?,
            request_date = ?,
            justification = ?,
            score = ?,
            score_breakdown_json = ?,
            rule_version = ?,
            proposed_status = ?,
            revised_status = ?,
            final_status = ?,
            reviewer_action = ?,
            reviewer_id = ?,
            reviewer_role = ?,
            review_notes = ?,
            notification_status = ?,
            payload_json = ?,
            updated_at = ?
        WHERE request_id = ? AND final_status = ?
        """,
        (
            *values[1:14],
            *values[14:24],
            values[25],
            values[0],
            expected_previous_status,
        ),
    )

    if cursor.rowcount != 1:
        raise ValueError(
            "Request was not updated because it is missing or its status has changed"
    )


def update_request_state(db_path, request, expected_previous_status):
    """Standalone conditional update; production transitions should also audit."""

    path = initialize_database(db_path)

    with closing(connect_db(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        persist_request_update(
            connection, request, expected_previous_status
        )


def persist_import_batch(
    db_path,
    *,
    batch_id,
    source_name,
    source_sha256,
    actor_user_id,
    total_records,
    processed_requests,
    rejected_records,
):
    """Atomically persist an import batch, its requests, rejects, and audit event."""

    import re
    from src.audit import append_audit_event

    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise ValueError("source_sha256 must be a SHA-256 hex digest")

    path = initialize_database(db_path)
    inserted = 0
    skipped_ids = []

    with closing(connect_db(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")

        actor = connection.execute(
            """SELECT role, is_active FROM users WHERE user_id = ?""",
            (actor_user_id,),
        ).fetchone()

        if (
            actor is None
            or not actor["is_active"]
            or actor["role"] not in {"ADMIN", "LND_REVIEWER"}
        ):
            raise PermissionError("User is not authorized to import requests")

        existing_batch = connection.execute(
            """SELECT * FROM import_batches WHERE source_sha256 = ?""",
            (source_sha256,),
        ).fetchone()

        if existing_batch:
            append_audit_event(
                path,
                event_type="DUPLICATE_IMPORT_ATTEMPT",
                actor_id=actor_user_id,
                details={
                    "source_name": source_name,
                    "existing_batch_id": existing_batch["batch_id"],
                    "source_sha256": source_sha256,
                },
                connection=connection,
            )
            return {
                "duplicate_import": True,
                "batch_id": existing_batch["batch_id"],
                "total_records": existing_batch["total_records"],
                "inserted_records": existing_batch["inserted_records"],
                "skipped_records": existing_batch["skipped_records"],
                "valid_records": existing_batch["valid_records"],
                "rejected_records": existing_batch["rejected_records"],
            }

        connection.execute(
            """
            INSERT INTO import_batches (
                batch_id, source_name, total_records,
                inserted_records, skipped_records, created_at,
                source_sha256, valid_records, rejected_records, actor_id
            ) VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                source_name,
                total_records,
                utc_now(),
                source_sha256,
                len(processed_requests),
                len(rejected_records),
                actor_user_id,
            ),
        )

        for request in processed_requests:
            if _insert_request(connection, request):
                inserted += 1
            else:
                skipped_ids.append(request["request_id"])

        for rejected in rejected_records:
            record = {
                key: value for key, value in rejected.items()
                if key not in {"source_row", "rejection_reasons"}
            }
            request_id = record.get("request_id", "")
            if not isinstance(request_id, str):
                request_id = str(request_id)[:200]

            connection.execute(
                """
                INSERT INTO rejected_records (
                    batch_id, source_row, request_id,
                    reason, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    int(rejected.get("source_row", 0)),
                    request_id or None,
                    str(rejected.get("rejection_reasons", "Invalid record"))[:4000],
                    _json(record),
                    utc_now(),
                ),
            )

        connection.execute(
            """
            UPDATE import_batches
            SET inserted_records = ?, skipped_records = ?
            WHERE batch_id = ?
            """,
            (inserted, len(skipped_ids), batch_id),
        )

        append_audit_event(
            path,
            event_type="IMPORT_BATCH_PROCESSED",
            actor_id=actor_user_id,
            details={
                "batch_id": batch_id,
                "source_name": source_name,
                "total_records": total_records,
                "valid_records": len(processed_requests),
                "inserted_records": inserted,
                "skipped_records": len(skipped_ids),
                "rejected_records": len(rejected_records),
                "rule_versions": sorted({
                    item["rule_version"] for item in processed_requests
                }),
            },
            connection=connection,
        )

    return {
        "duplicate_import": False,
        "batch_id": batch_id,
        "total_records": total_records,
        "valid_records": len(processed_requests),
        "inserted_records": inserted,
        "skipped_records": len(skipped_ids),
        "rejected_records": len(rejected_records),
        "skipped_request_ids": skipped_ids,
    }


def list_import_batches(db_path, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")

    path = initialize_database(db_path)
    with closing(connect_db(path)) as connection:
        rows = connection.execute(
            """SELECT batch_id, source_name, total_records,
                      valid_records, inserted_records, skipped_records,
                      rejected_records, actor_id, created_at
               FROM import_batches ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


def list_rejected_records(db_path, batch_id=None, limit=1000):
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")

    path = initialize_database(db_path)
    with closing(connect_db(path)) as connection:
        if batch_id:
            rows = connection.execute(
                """SELECT * FROM rejected_records
                   WHERE batch_id = ?
                   ORDER BY rejection_id LIMIT ?""",
                (batch_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM rejected_records
                   ORDER BY rejection_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    results = []

    for row in rows:
        item = dict(row)
        item["record"] = json.loads(item.pop("record_json"))
        results.append(item)

    return results


def get_notification(db_path, notification_id):
    path = initialize_database(db_path)

    with closing(connect_db(path)) as connection:
        row = connection.execute(
            """SELECT * FROM notification_drafts
               WHERE notification_id = ?""",
            (notification_id,),
        ).fetchone()

    return dict(row) if row else None


def list_notification_attempts(db_path, notification_id):
    path = initialize_database(db_path)

    with closing(connect_db(path)) as connection:
        rows = connection.execute(
            """SELECT attempt_id, notification_id, attempt_number,
                      actor_id, timestamp, result, error
               FROM notification_attempts
               WHERE notification_id = ?
               ORDER BY attempt_number""",
            (notification_id,),
        ).fetchall()

    return [dict(row) for row in rows]
