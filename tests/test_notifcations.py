
import pytest

from src.authentication import bootstrap_admin, create_user
from src.database import initialize_database, save_processed_requests
from src.notifications import (
    approve_notification_draft,
    dispatch_notification,
    generate_notification_draft,
)
from src.workflow import run_workflow, apply_review_action


RULES = {
    "rule_version": "1.0",
    "criteria": {
        "role_relevance_score": 40,
        "skill_gap_alignment_score": 35,
        "prerequisite_completion_score": 25,
    },
    "thresholds": {
        "manager_approval_review": 75,
        "lnd_review": 50,
    },
    "allowed_roles": ["Software Engineer", "Data Analyst"],
    "sla_hours": 48,
    "notification_max_retries": 2,
}


def make_record(request_id="TR0001", score=90):
    return {
        "request_id": request_id,
        "employee_id": "E0001",
        "employee_name": "Aarav Shah",
        "email": "aarav@example.com",
        "employee_role": "Software Engineer",
        "training_course": "Secure API Development",
        "role_relevance_score": score,
        "skill_gap_alignment_score": score,
        "prerequisite_completion_score": score,
        "manager_id": "M001",
        "request_date": "2026-10-20",
        "justification": "A synthetic training request.",
    }


def setup(tmp_path, score=90):
    db_path = tmp_path / "trainflow.db"
    initialize_database(db_path)

    admin = bootstrap_admin(
        db_path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        db_path,
        admin["user_id"],
        "trainingmanager",
        "Manager-Secure-Password-123!",
        "MANAGER",
    )

    lnd = create_user(
        db_path,
        admin["user_id"],
        "lndreviewer",
        "Lnd-Secure-Password-123!",
        "LND_REVIEWER",
    )

    processed = run_workflow(
        [make_record(score=score)],
        RULES,
    )["processed_requests"]

    save_processed_requests(db_path, processed)

    return db_path, admin, manager, lnd


def test_draft_starts_as_draft_and_has_no_score(tmp_path):
    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    assert draft["status"] == "DRAFT"
    assert "89" not in draft["message"]
    assert "approved" not in draft["message"].lower()


def test_dispatch_is_blocked_before_message_approval(tmp_path):
    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    with pytest.raises(ValueError, match="approved before dispatch"):
        dispatch_notification(
            db_path,
            draft["notification_id"],
            manager["user_id"],
        )


def test_approval_and_simulated_retry_limit(tmp_path):
    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    approved = approve_notification_draft(
        db_path,
        draft["notification_id"],
        manager["user_id"],
    )

    assert approved["status"] == "APPROVED"
    assert approved["approved_by"] == manager["user_id"]
    assert approved["approved_content_hash"] == approved["content_hash"]

    first = dispatch_notification(
        db_path,
        approved["notification_id"],
        manager["user_id"],
        simulate_failure=True,
    )
    assert first["status"] == "FAILED"
    assert first["attempts"] == 1

    second = dispatch_notification(
        db_path,
        approved["notification_id"],
        manager["user_id"],
        simulate_failure=True,
    )
    assert second["status"] == "FAILED"
    assert second["attempts"] == 2

    third = dispatch_notification(
        db_path,
        approved["notification_id"],
        manager["user_id"],
        simulate_failure=False,
    )
    assert third["status"] == "SENT"
    assert third["attempts"] == 3

    with pytest.raises(ValueError):
        dispatch_notification(
            db_path,
            approved["notification_id"],
            manager["user_id"],
        )


def test_unrelated_role_cannot_approve_manager_queue_message(tmp_path):
    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    with pytest.raises(PermissionError):
        approve_notification_draft(
            db_path,
            draft["notification_id"],
            lnd["user_id"],
        )


def test_stale_draft_cannot_be_approved(tmp_path):
    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    apply_review_action(
        db_path,
        "TR0001",
        manager["user_id"],
        "APPROVE",
        notes="Request approved by manager.",
    )

    with pytest.raises(ValueError, match="state changed"):
        approve_notification_draft(
            db_path,
            draft["notification_id"],
            manager["user_id"],
        )


def test_unapproved_message_content_cannot_be_changed(tmp_path):
    import sqlite3

    db_path, admin, manager, lnd = setup(tmp_path)

    draft = generate_notification_draft(
        db_path, "TR0001", manager["user_id"]
    )

    approved = approve_notification_draft(
        db_path,
        draft["notification_id"],
        manager["user_id"],
    )

    # Simulate database tampering by a privileged actor.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE notification_drafts
            SET message = ?
            WHERE notification_id = ?
            """,
            (
                "Tampered message",
                approved["notification_id"],
            ),
        )

    with pytest.raises(ValueError):
        dispatch_notification(
            db_path,
            approved["notification_id"],
            manager["user_id"],
        )
