
from src.database import (
    get_request,
    initialize_database,
    list_requests,
    save_processed_requests,
    update_request_state,
)


def make_request(request_id="TR0001"):
    return {
        "request_id": request_id,
        "employee_id": "E0001",
        "employee_name": "Aarav Shah",
        "email": "aarav@example.com",
        "employee_role": "Software Engineer",
        "training_course": "Secure API Development",
        "role_relevance_score": 90.0,
        "skill_gap_alignment_score": 80.0,
        "prerequisite_completion_score": 100.0,
        "manager_id": "M001",
        "request_date": "2026-10-20",
        "justification": "Supports my current development plan.",
        "score": 89.0,
        "score_breakdown": {
            "role_relevance_score": {
                "input_score": 90.0,
                "weight_percent": 40.0,
                "weighted_contribution": 36.0,
            },
            "skill_gap_alignment_score": {
                "input_score": 80.0,
                "weight_percent": 35.0,
                "weighted_contribution": 28.0,
            },
            "prerequisite_completion_score": {
                "input_score": 100.0,
                "weight_percent": 25.0,
                "weighted_contribution": 25.0,
            },
        },
        "rule_version": "1.0",
        "proposed_status": "MANAGER_APPROVAL_REVIEW",
        "revised_status": None,
        "final_status": "PENDING_REVIEW",
        "reviewer_action": None,
        "reviewer_id": None,
        "reviewer_role": None,
        "review_notes": None,
        "notification_status": "DRAFT",
    }


def test_database_initialization(tmp_path):
    path = tmp_path / "trainflow.db"

    result = initialize_database(path)

    assert result.exists()


def test_request_is_persisted_and_retrieved(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    result = save_processed_requests(path, [make_request()])

    assert result["inserted"] == 1
    assert result["skipped"] == 0

    request = get_request(path, "TR0001")

    assert request is not None
    assert request["score"] == 89.0
    assert request["final_status"] == "PENDING_REVIEW"
    assert request["score_breakdown"]["role_relevance_score"][
        "weighted_contribution"
    ] == 36.0


def test_repeated_import_does_not_overwrite_existing_request(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    original = make_request()
    save_processed_requests(path, [original])

    changed = make_request()
    changed["score"] = 20.0

    result = save_processed_requests(path, [changed])

    assert result["inserted"] == 0
    assert result["skipped"] == 1

    stored = get_request(path, "TR0001")
    assert stored["score"] == 89.0


def test_list_requests_can_filter_by_status(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    first = make_request("TR0001")
    second = make_request("TR0002")
    second["final_status"] = "ON_HOLD"

    save_processed_requests(path, [first, second])

    held = list_requests(path, final_status="ON_HOLD")

    assert len(held) == 1
    assert held[0]["request_id"] == "TR0002"


def test_update_requires_expected_previous_status(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    request = make_request()
    save_processed_requests(path, [request])

    request["final_status"] = "ON_HOLD"
    request["reviewer_action"] = "HOLD"
    request["reviewer_id"] = "user-123"
    request["reviewer_role"] = "MANAGER"
    request["review_notes"] = "Waiting for the course schedule."

    update_request_state(
        path,
        request,
        expected_previous_status="PENDING_REVIEW",
    )

    stored = get_request(path, "TR0001")
    assert stored["final_status"] == "ON_HOLD"


def test_stale_update_is_rejected(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    request = make_request()
    save_processed_requests(path, [request])

    request["final_status"] = "ON_HOLD"
    request["reviewer_action"] = "HOLD"
    request["reviewer_id"] = "user-123"
    request["reviewer_role"] = "MANAGER"
    request["review_notes"] = "Waiting."

    update_request_state(
        path,
        request,
        expected_previous_status="PENDING_REVIEW",
    )

    request["final_status"] = "APPROVED"

    try:
        update_request_state(
            path,
            request,
            expected_previous_status="PENDING_REVIEW",
        )
        assert False, "Expected stale update to fail"
    except ValueError as exc:
        assert "status has changed" in str(exc)


def test_missing_request_returns_none(tmp_path):
    path = tmp_path / "trainflow.db"
    initialize_database(path)

    assert get_request(path, "DOES_NOT_EXIST") is None
