
"""TrainFlow: request processing, import workflow, and secure review actions."""

import csv
import hashlib
import uuid
from copy import deepcopy
from pathlib import Path

from src.audit import append_audit_event
from src.authentication import get_active_identity, require_roles
from src.database import (
    connect_db,
    initialize_database,
    list_import_batches,
    list_rejected_records,
    persist_import_batch,
    persist_request_update,
    _row_to_request,
    utc_now,
)
from src.scoring import (
    ROUTING_STATUSES,
    calculate_score,
    load_rules,
    route_score,
    validate_rules,
)
from src.validation import load_records, validate_records


REVIEW_ACTIONS = {"APPROVE", "EDIT", "HOLD", "REQUEST_INFO"}
REVIEWABLE_STATES = {"PENDING_REVIEW", "REQUIRES_REVIEW"}
REOPENABLE_STATES = {"ON_HOLD", "AWAITING_INFORMATION"}



def run_workflow(records, rules):
    """Validate and score requests using role-specific routing rules."""

    validate_rules(rules)

    valid_records, rejected_records = validate_records(
        records,
        allowed_roles=rules["allowed_roles"],
    )

    processed_requests = []

    for record in valid_records:
        evaluation = calculate_score(record, rules)

        proposed_status = route_score(
            evaluation["score"],
            rules,
            employee_role=record["employee_role"],
        )

        processed_requests.append({
            **record,
            "score": evaluation["score"],
            "score_breakdown": evaluation["score_breakdown"],
            "rule_version": evaluation["rule_version"],
            "proposed_status": proposed_status,
            "revised_status": None,
            "final_status": "PENDING_REVIEW",
            "reviewer_action": None,
            "reviewer_id": None,
            "reviewer_role": None,
            "review_notes": None,
            "review_history": [],
            "notification_status": "DRAFT",
        })

    status_counts = {
        status: sum(
            request["proposed_status"] == status
            for request in processed_requests
        )
        for status in sorted(ROUTING_STATUSES)
    }

    return {
        "processed_requests": processed_requests,
        "rejected_records": rejected_records,
        "summary": {
            "total_records": len(records),
            "valid_records": len(records) - len(rejected_records),
            "rejected_records": len(rejected_records),
            "processed_requests": len(processed_requests),
            "pending_human_review": len(processed_requests),
            "proposed_status_counts": status_counts,
            "rule_version": rules["rule_version"],
        },
    }



def process_import_file(
    file_path,
    db_path,
    actor_user_id,
    rules_path=None,
):
    """
    Process one uploaded CSV/JSON file and persist the entire batch.

    The importer requires an active ADMIN or LND_REVIEWER identity.
    """

    identity = require_roles(
        db_path,
        actor_user_id,
        {"ADMIN", "LND_REVIEWER"},
    )

    path = Path(file_path)

    # load_records performs extension, size, header, and syntax checks.
    records = load_records(path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    rules = load_rules(rules_path)
    import time
    import streamlit as st

    start_time = time.perf_counter()

    result = run_workflow(records, rules)

    elapsed_seconds = time.perf_counter() - start_time

    processed_count = len(result.get("processed_requests", []))

    if processed_count > 0:
        average_seconds = elapsed_seconds / processed_count

        st.session_state["workflow_timing_metrics"] = {
            "average_processing_seconds_per_valid_record": average_seconds,
            "average_processing_milliseconds_per_valid_record": (
                average_seconds * 1000
            ),
        }

    persistence = persist_import_batch(
        db_path,
        batch_id=str(uuid.uuid4()),
        source_name=path.name,
        source_sha256=source_hash,
        actor_user_id=identity["user_id"],
        total_records=len(records),
        processed_requests=result["processed_requests"],
        rejected_records=result["rejected_records"],
    )

    result["persistence"] = persistence
    result["summary"]["duplicate_import"] = persistence["duplicate_import"]

    if not persistence["duplicate_import"]:
        result["summary"]["inserted_records"] = persistence["inserted_records"]
        result["summary"]["skipped_existing_records"] = persistence["skipped_records"]
        result["summary"]["batch_id"] = persistence["batch_id"]
    else:
        result["summary"]["batch_id"] = persistence["batch_id"]
        result["summary"]["message"] = (
            "This exact file content was imported previously. "
            "No new records were created."
        )

    return result


def _request_from_connection(connection, request_id):
    row = connection.execute(
        """SELECT * FROM training_requests WHERE request_id = ?""",
        (request_id,),
    ).fetchone()

    return _row_to_request(row) if row else None


def _authenticated_actor(connection, actor_user_id, expected_identity):
    """Re-check identity inside the write transaction to reduce race conditions."""

    actor = connection.execute(
        """SELECT user_id, username, role, is_active
           FROM users WHERE user_id = ?""",
        (actor_user_id,),
    ).fetchone()

    if actor is None or not actor["is_active"]:
        raise PermissionError("User is missing or inactive")

    if actor["role"] != expected_identity["role"]:
        raise PermissionError("User role changed; authenticate again")

    return {
        "user_id": actor["user_id"],
        "username": actor["username"],
        "role": actor["role"],
    }


def _target_status(request):
    return request.get("revised_status") or request["proposed_status"]


def _check_queue_permission(request, actor_role):
    target = _target_status(request)

    if target == "MANAGER_APPROVAL_REVIEW":
        allowed = {"MANAGER", "ADMIN"}
    elif target in {"LND_REVIEW", "NEEDS_JUSTIFICATION_REVIEW"}:
        allowed = {"LND_REVIEWER", "ADMIN"}
    else:
        raise ValueError(f"Unsupported review queue: {target}")

    if actor_role not in allowed:
        raise PermissionError(
            f"Role {actor_role} cannot review queue {target}"
        )


def apply_review_action(
    db_path,
    request_id,
    actor_user_id,
    action,
    notes="",
    override_status=None,
):
    """
    Apply an authenticated review action and atomically save its audit event.

    Do not pass reviewer_role or a mutable request object to this function.
    """

    identity = get_active_identity(db_path, actor_user_id)

    if identity is None:
        raise PermissionError("User is missing or inactive")

    action = action.strip().upper() if isinstance(action, str) else ""

    if action not in REVIEW_ACTIONS:
        raise ValueError(f"Unsupported review action: {action}")

    if not isinstance(notes, str):
        raise ValueError("Review notes must be text")

    notes = notes.strip()

    if len(notes) > 2000:
        raise ValueError("Review notes exceed 2000 characters")

    if any(ord(char) < 32 and char not in "\n\r\t" for char in notes):
        raise ValueError("Review notes contain invalid control characters")

    if action == "EDIT":
        if not notes:
            raise ValueError("EDIT requires a documented reason")
        if override_status not in ROUTING_STATUSES:
            raise ValueError("EDIT requires a valid revised routing status")
    elif override_status is not None:
        raise ValueError("override_status is only valid for EDIT")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        actor = _authenticated_actor(
            connection, actor_user_id, identity
        )
        request = _request_from_connection(connection, request_id)

        if request is None:
            raise ValueError("Training request does not exist")

        old_status = request["final_status"]

        if old_status not in REVIEWABLE_STATES:
            raise ValueError(
                f"Cannot review a request in state {old_status}"
            )

        _check_queue_permission(request, actor["role"])

        updated = deepcopy(request)

        if action == "APPROVE":
            new_status = "APPROVED"
        elif action == "EDIT":
            new_status = "REQUIRES_REVIEW"
            updated["revised_status"] = override_status
        elif action == "HOLD":
            new_status = "ON_HOLD"
        else:
            new_status = "AWAITING_INFORMATION"

        timestamp = utc_now()

        updated["final_status"] = new_status
        updated["reviewer_action"] = action
        updated["reviewer_id"] = actor["user_id"]
        updated["reviewer_role"] = actor["role"]
        updated["review_notes"] = notes
        updated["updated_at"] = timestamp

        history = updated.get("review_history", [])
        if not isinstance(history, list):
            raise ValueError("Invalid review history")

        history.append({
            "timestamp": timestamp,
            "reviewer_id": actor["user_id"],
            "reviewer_role": actor["role"],
            "action": action,
            "previous_status": old_status,
            "new_status": new_status,
            "revised_status": updated.get("revised_status"),
            "notes": notes,
        })

        updated["review_history"] = history

        persist_request_update(
            connection,
            updated,
            expected_previous_status=old_status,
        )

        append_audit_event(
            path,
            event_type="REQUEST_REVIEWED",
            actor_id=actor["user_id"],
            request_id=request_id,
            details={
                "action": action,
                "previous_status": old_status,
                "new_status": new_status,
                "proposed_status": request["proposed_status"],
                "revised_status": updated.get("revised_status"),
                "rule_version": request["rule_version"],
                "notes": notes,
            },
            connection=connection,
        )

    return updated


def reopen_request(
    db_path,
    request_id,
    actor_user_id,
    notes,
):
    """Reopen a held or information-pending request for human review."""

    identity = get_active_identity(db_path, actor_user_id)

    if identity is None:
        raise PermissionError("User is missing or inactive")

    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("A reason is required to reopen a request")

    notes = notes.strip()

    if len(notes) > 2000:
        raise ValueError("Review notes exceed 2000 characters")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        actor = _authenticated_actor(
            connection, actor_user_id, identity
        )
        request = _request_from_connection(connection, request_id)

        if request is None:
            raise ValueError("Training request does not exist")

        old_status = request["final_status"]

        if old_status not in {"ON_HOLD", "AWAITING_INFORMATION"}:
            raise ValueError("Request is not in a reopenable state")

        _check_queue_permission(request, actor["role"])

        updated = deepcopy(request)
        timestamp = utc_now()

        updated.update({
            "final_status": "PENDING_REVIEW",
            "reviewer_action": "REOPEN",
            "reviewer_id": actor["user_id"],
            "reviewer_role": actor["role"],
            "review_notes": notes,
            "updated_at": timestamp,
        })

        history = updated.get("review_history", [])
        history.append({
            "timestamp": timestamp,
            "reviewer_id": actor["user_id"],
            "reviewer_role": actor["role"],
            "action": "REOPEN",
            "previous_status": old_status,
            "new_status": "PENDING_REVIEW",
            "notes": notes,
        })
        updated["review_history"] = history

        persist_request_update(
            connection,
            updated,
            expected_previous_status=old_status,
        )

        append_audit_event(
            path,
            event_type="REQUEST_REOPENED",
            actor_id=actor["user_id"],
            request_id=request_id,
            details={
                "previous_status": old_status,
                "new_status": "PENDING_REVIEW",
                "notes": notes,
            },
            connection=connection,
        )

    return updated


def get_rejected_records_for_user(
    db_path,
    actor_user_id,
    batch_id=None,
    limit=1000,
):
    """Restrict rejected-record access to authorized HR/L&D reviewers."""

    require_roles(
        db_path,
        actor_user_id,
        {"ADMIN", "LND_REVIEWER"},
    )

    return list_rejected_records(
        db_path,
        batch_id=batch_id,
        limit=limit,
    )


def get_import_batches_for_user(
    db_path,
    actor_user_id,
    limit=100,
):
    require_roles(
        db_path,
        actor_user_id,
        {"ADMIN", "LND_REVIEWER"},
    )

    return list_import_batches(db_path, limit=limit)


def export_rejected_records_csv(
    db_path,
    actor_user_id,
    output_path,
    batch_id=None,
):
    """
    Export rejected records for an authorized reviewer.

    CSV formula-like text is neutralized in the export so opening it in
    spreadsheet software cannot directly evaluate user-supplied formulas.
    """

    rejected = get_rejected_records_for_user(
        db_path,
        actor_user_id,
        batch_id=batch_id,
    )

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def safe_cell(value):
        text = "" if value is None else str(value)
        if text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        fields = [
            "rejection_id", "batch_id", "source_row",
            "request_id", "reason", "record",
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for item in rejected:
            writer.writerow({
                "rejection_id": item["rejection_id"],
                "batch_id": item["batch_id"],
                "source_row": item["source_row"],
                "request_id": safe_cell(item["request_id"]),
                "reason": safe_cell(item["reason"]),
                "record": safe_cell(
                    __import__("json").dumps(
                        item["record"], ensure_ascii=False
                    )
                ),
            })

    return path
