
import sqlite3

from src.audit import (
    append_audit_event,
    verify_audit_chain,
)
from src.database import initialize_database


def test_empty_chain_is_valid(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    result = verify_audit_chain(path)

    assert result["valid"] is True
    assert result["events_checked"] == 0


def test_appended_events_form_valid_chain(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    append_audit_event(
        path,
        event_type="REQUEST_IMPORTED",
        actor_id="admin-001",
        request_id="TR0001",
        details={"rule_version": "1.0"},
    )

    append_audit_event(
        path,
        event_type="REQUEST_APPROVED",
        actor_id="manager-001",
        request_id="TR0001",
        details={"previous_status": "PENDING_REVIEW"},
    )

    result = verify_audit_chain(path)

    assert result["valid"] is True
    assert result["events_checked"] == 2
    assert len(result["latest_hash"]) == 64


def test_sensitive_values_are_redacted(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    append_audit_event(
        path,
        event_type="SECURITY_TEST",
        details={
            "username": "testuser",
            "password": "DoNotStoreThis",
            "api_key": "fake-key",
        },
    )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT details_json FROM audit_events"
        ).fetchone()

    assert "DoNotStoreThis" not in row[0]
    assert "fake-key" not in row[0]
    assert "[REDACTED]" in row[0]


def test_modified_event_fails_chain_verification(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    append_audit_event(
        path,
        event_type="REQUEST_IMPORTED",
        details={"source": "training_requests.csv"},
    )

    append_audit_event(
        path,
        event_type="REQUEST_REVIEWED",
        details={"action": "HOLD"},
    )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            """
            UPDATE audit_events
            SET details_json = ?
            WHERE event_id = 1
            """,
            ('{"source":"tampered.csv"}',),
        )

    result = verify_audit_chain(path)

    assert result["valid"] is False
    assert result["first_invalid_event_id"] == 1


def test_invalid_event_type_is_rejected(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    try:
        append_audit_event(
            path,
            event_type="invalid event type with spaces",
        )
        assert False, "Expected invalid event type to fail"
    except ValueError as exc:
        assert "event_type" in str(exc)


def test_oversized_audit_details_are_rejected(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    try:
        append_audit_event(
            path,
            event_type="OVERSIZED_EVENT",
            details={"message": "X" * 30_000},
        )
        assert False, "Expected oversized audit details to fail"
    except ValueError as exc:
        assert "size limit" in str(exc)
