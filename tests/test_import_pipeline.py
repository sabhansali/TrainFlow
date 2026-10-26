
import csv
import json

import pytest

from src.authentication import bootstrap_admin, create_user
from src.database import (
    get_request,
    initialize_database,
    list_import_batches,
)
from src.workflow import (
    get_rejected_records_for_user,
    process_import_file,
)


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


def record(request_id, email="person@example.com", score=80):
    return {
        "request_id": request_id,
        "employee_id": "E0001",
        "employee_name": "Test Person",
        "email": email,
        "employee_role": "Software Engineer",
        "training_course": "Secure API Development",
        "role_relevance_score": score,
        "skill_gap_alignment_score": score,
        "prerequisite_completion_score": score,
        "manager_id": "M001",
        "request_date": "2026-10-20",
        "justification": "A synthetic test record.",
    }


def setup_admin(tmp_path):
    db_path = tmp_path / "trainflow.db"
    initialize_database(db_path)

    admin = bootstrap_admin(
        db_path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    return db_path, admin


def write_csv(path, records):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)


def test_import_persists_valid_and_rejected_records(tmp_path):
    db_path, admin = setup_admin(tmp_path)

    source = tmp_path / "training_requests.csv"
    write_csv(source, [
        record("TR0001"),
        record("TR0002", email=""),
        record("TR0003", score=101),
    ])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(RULES), encoding="utf-8")

    result = process_import_file(
        source,
        db_path,
        admin["user_id"],
        rules_path=rules_path,
    )

    assert result["summary"]["total_records"] == 3
    assert result["summary"]["valid_records"] == 1
    assert result["summary"]["rejected_records"] == 2
    assert result["persistence"]["inserted_records"] == 1

    assert get_request(db_path, "TR0001") is not None
    assert get_request(db_path, "TR0002") is None

    rejected = get_rejected_records_for_user(
        db_path, admin["user_id"]
    )
    assert len(rejected) == 2

    batches = list_import_batches(db_path)
    assert len(batches) == 1


def test_same_file_content_is_not_imported_twice(tmp_path):
    db_path, admin = setup_admin(tmp_path)

    source = tmp_path / "training_requests.csv"
    write_csv(source, [record("TR0001")])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(RULES), encoding="utf-8")

    first = process_import_file(
        source, db_path, admin["user_id"], rules_path
    )
    second = process_import_file(
        source, db_path, admin["user_id"], rules_path
    )

    assert first["persistence"]["duplicate_import"] is False
    assert second["persistence"]["duplicate_import"] is True
    assert len(list_import_batches(db_path)) == 1


def test_lnd_reviewer_can_import(tmp_path):
    db_path, admin = setup_admin(tmp_path)

    lnd = create_user(
        db_path,
        admin["user_id"],
        "lndreviewer",
        "Lnd-Secure-Password-123!",
        "LND_REVIEWER",
    )

    source = tmp_path / "training_requests.csv"
    write_csv(source, [record("TR0001")])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(RULES), encoding="utf-8")

    result = process_import_file(
        source, db_path, lnd["user_id"], rules_path
    )

    assert result["persistence"]["inserted_records"] == 1


def test_employee_cannot_import(tmp_path):
    db_path, admin = setup_admin(tmp_path)

    employee = create_user(
        db_path,
        admin["user_id"],
        "employeeuser",
        "Employee-Secure-Password-123!",
        "EMPLOYEE",
    )

    source = tmp_path / "training_requests.csv"
    write_csv(source, [record("TR0001")])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(RULES), encoding="utf-8")

    with pytest.raises(PermissionError):
        process_import_file(
            source, db_path, employee["user_id"], rules_path
        )
