
"""TrainFlow: approval-gated notification drafts and simulated delivery."""

import hashlib
import hmac
import re
import uuid
from copy import deepcopy

from src.audit import append_audit_event
from src.authentication import get_active_identity
from src.database import (
    connect_db,
    initialize_database,
    persist_request_update,
    utc_now,
    _row_to_request,
)
from src.workflow import _authenticated_actor, _check_queue_permission


MAX_TOTAL_ATTEMPTS = 3


def _clean_text(value, max_length=180):
    if not isinstance(value, str):
        value = str(value or "")
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return " ".join(value.split())[:max_length]


def _build_message(request):
    name = _clean_text(request["employee_name"], 100)
    course = _clean_text(request["training_course"], 160)
    status = request["final_status"]
    target = request.get("revised_status") or request["proposed_status"]

    if status == "APPROVED":
        return (
            "Training Request Approved",
            f"Dear {name},\n\nYour request for {course} has been approved. "
            "The Learning and Development team will share the next steps.",
        )

    if status == "ON_HOLD":
        return (
            "Training Request On Hold",
            f"Dear {name},\n\nYour request for {course} is temporarily "
            "on hold pending further review. You will receive an update "
            "when the review resumes.",
        )

    if status == "AWAITING_INFORMATION":
        return (
            "Additional Information Required",
            f"Dear {name},\n\nThe team requires additional information "
            f"about your request for {course}. Please contact the "
            "designated reviewer.",
        )

    if status == "REQUIRES_REVIEW":
        return (
            "Training Request Under Further Review",
            f"Dear {name},\n\nYour request for {course} requires further "
            "review. No final outcome has been communicated.",
        )

    if target == "MANAGER_APPROVAL_REVIEW":
        return (
            "Training Request Received",
            f"Dear {name},\n\nYour request for {course} has been recorded "
            "and is awaiting manager review. This is not a final approval.",
        )

    if target == "LND_REVIEW":
        return (
            "Training Request Under Review",
            f"Dear {name},\n\nYour request for {course} has been forwarded "
            "for Learning and Development review. This is not a final decision.",
        )

    return (
        "Training Request Requires Further Review",
        f"Dear {name},\n\nYour request for {course} requires additional "
        "review or justification. No rejection has been issued.",
    )


def _content_hash(subject, message):
    return hashlib.sha256(
        (subject + "\n" + message).encode("utf-8")
    ).hexdigest()


def _verify_stored_message(draft):
    """Ensure the subject and body still match the recorded content hash."""

    actual_hash = _content_hash(draft["subject"], draft["message"])

    if not hmac.compare_digest(actual_hash, draft["content_hash"]):
        raise ValueError(
            "Stored notification content does not match its recorded hash"
        )

    return actual_hash


def _get_draft(connection, notification_id):
    row = connection.execute(
        "SELECT * FROM notification_drafts WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()
    return dict(row) if row else None


def _set_request_notification_status(connection, request, new_status):
    previous_final_status = request["final_status"]
    updated = deepcopy(request)
    updated["notification_status"] = new_status
    updated["updated_at"] = utc_now()

    persist_request_update(
        connection,
        updated,
        expected_previous_status=previous_final_status,
    )


def generate_notification_draft(db_path, request_id, actor_user_id):
    identity = get_active_identity(db_path, actor_user_id)
    if identity is None:
        raise PermissionError("User is missing or inactive")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticated_actor(connection, actor_user_id, identity)

        row = connection.execute(
            "SELECT * FROM training_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Training request does not exist")

        request = _row_to_request(row)
        _check_queue_permission(request, actor["role"])

        subject, message = _build_message(request)
        digest = _content_hash(subject, message)

        existing = connection.execute(
            """
            SELECT * FROM notification_drafts
            WHERE request_id = ? AND content_hash = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (request_id, digest),
        ).fetchone()

        if existing:
            existing_draft = dict(existing)
            _verify_stored_message(existing_draft)

            append_audit_event(
                path,
                "NOTIFICATION_DRAFT_REUSED",
                actor_id=actor["user_id"],
                request_id=request_id,
                details={
                    "notification_id": existing_draft["notification_id"],
                    "status": existing_draft["status"],
                },
                connection=connection,
            )
            return existing_draft

        notification_id = str(uuid.uuid4())
        timestamp = utc_now()

        connection.execute(
            """
            INSERT INTO notification_drafts (
                notification_id, request_id, subject, message,
                content_hash, status, created_by, approved_by,
                approved_role, approved_at, approved_content_hash,
                attempts, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, NULL, NULL, NULL,
                      NULL, 0, NULL, ?, ?)
            """,
            (
                notification_id, request_id, subject, message, digest,
                actor["user_id"], timestamp, timestamp,
            ),
        )

        _set_request_notification_status(connection, request, "DRAFT")

        append_audit_event(
            path,
            "NOTIFICATION_DRAFT_CREATED",
            actor_id=actor["user_id"],
            request_id=request_id,
            details={
                "notification_id": notification_id,
                "content_hash": digest,
            },
            connection=connection,
        )

        result = _get_draft(connection, notification_id)

    return result


def approve_notification_draft(db_path, notification_id, actor_user_id):
    identity = get_active_identity(db_path, actor_user_id)
    if identity is None:
        raise PermissionError("User is missing or inactive")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticated_actor(connection, actor_user_id, identity)

        draft = _get_draft(connection, notification_id)
        if draft is None:
            raise ValueError("Notification draft does not exist")

        if draft["status"] != "DRAFT":
            raise ValueError("Only DRAFT messages can be approved")

        # Validate the actual stored message before approving it.
        stored_hash = _verify_stored_message(draft)

        row = connection.execute(
            "SELECT * FROM training_requests WHERE request_id = ?",
            (draft["request_id"],),
        ).fetchone()

        if row is None:
            raise ValueError("Parent training request does not exist")

        request = _row_to_request(row)
        _check_queue_permission(request, actor["role"])

        current_subject, current_message = _build_message(request)
        current_hash = _content_hash(current_subject, current_message)

        if (
            not hmac.compare_digest(current_hash, stored_hash)
            or draft["subject"] != current_subject
            or draft["message"] != current_message
        ):
            raise ValueError(
                "Workflow state changed after draft generation. "
                "Generate and review a new draft."
            )

        timestamp = utc_now()

        connection.execute(
            """
            UPDATE notification_drafts
            SET status = 'APPROVED',
                approved_by = ?,
                approved_role = ?,
                approved_at = ?,
                approved_content_hash = ?,
                updated_at = ?
            WHERE notification_id = ? AND status = 'DRAFT'
            """,
            (
                actor["user_id"], actor["role"], timestamp,
                stored_hash, timestamp, notification_id,
            ),
        )

        _set_request_notification_status(connection, request, "APPROVED")

        append_audit_event(
            path,
            "NOTIFICATION_APPROVED",
            actor_id=actor["user_id"],
            request_id=draft["request_id"],
            details={
                "notification_id": notification_id,
                "approved_content_hash": stored_hash,
            },
            connection=connection,
        )

        result = _get_draft(connection, notification_id)

    return result


def dispatch_notification(
    db_path,
    notification_id,
    actor_user_id,
    *,
    simulate_failure=False,
):
    """
    Execute one simulated delivery attempt; no real email is sent.

    Three total attempts are permitted: one initial attempt plus two retries.
    """

    if not isinstance(simulate_failure, bool):
        raise ValueError("simulate_failure must be boolean")

    identity = get_active_identity(db_path, actor_user_id)
    if identity is None:
        raise PermissionError("User is missing or inactive")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticated_actor(connection, actor_user_id, identity)

        draft = _get_draft(connection, notification_id)
        if draft is None:
            raise ValueError("Notification draft does not exist")

        if draft["status"] not in {"APPROVED", "FAILED"}:
            raise ValueError(
                "Notification must be approved before dispatch or retry"
            )

        if draft["attempts"] >= MAX_TOTAL_ATTEMPTS:
            raise ValueError(
                "Maximum reached: one initial attempt plus two retries"
            )

        row = connection.execute(
            "SELECT * FROM training_requests WHERE request_id = ?",
            (draft["request_id"],),
        ).fetchone()

        if row is None:
            raise ValueError("Parent training request does not exist")

        request = _row_to_request(row)
        _check_queue_permission(request, actor["role"])

        # Validate both stored text and the approval against current state.
        stored_hash = _verify_stored_message(draft)
        subject, message = _build_message(request)
        current_hash = _content_hash(subject, message)

        if (
            not hmac.compare_digest(current_hash, stored_hash)
            or not hmac.compare_digest(
                current_hash,
                draft["approved_content_hash"] or "",
            )
        ):
            raise ValueError(
                "Message content or approval no longer matches the workflow"
            )

        attempt_number = draft["attempts"] + 1
        timestamp = utc_now()

        if simulate_failure:
            result = "FAILED"
            error = "Simulated delivery failure"
        else:
            result = "SENT"
            error = None

        connection.execute(
            """
            INSERT INTO notification_attempts (
                notification_id, attempt_number, actor_id,
                timestamp, result, error
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                notification_id, attempt_number, actor["user_id"],
                timestamp, result, error,
            ),
        )

        connection.execute(
            """
            UPDATE notification_drafts
            SET status = ?, attempts = ?, last_error = ?, updated_at = ?
            WHERE notification_id = ?
            """,
            (result, attempt_number, error, timestamp, notification_id),
        )

        _set_request_notification_status(connection, request, result)

        append_audit_event(
            path,
            "NOTIFICATION_ATTEMPT",
            actor_id=actor["user_id"],
            request_id=draft["request_id"],
            details={
                "notification_id": notification_id,
                "attempt_number": attempt_number,
                "result": result,
                "error": error,
                "simulated": True,
            },
            connection=connection,
        )

        updated = _get_draft(connection, notification_id)

    return updated
