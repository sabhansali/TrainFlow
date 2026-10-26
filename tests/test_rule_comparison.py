
from pathlib import Path

import pytest

from src.scoring import (
    calculate_score,
    compare_rule_versions,
    load_rule_version,
    route_score,
    validate_rules,
)


ROOT = Path(__file__).resolve().parent.parent


def make_record(
    request_id,
    role,
    relevance,
    skill_gap,
    prerequisites,
):
    return {
        "request_id": request_id,
        "employee_id": "E0001",
        "employee_role": role,
        "role_relevance_score": relevance,
        "skill_gap_alignment_score": skill_gap,
        "prerequisite_completion_score": prerequisites,
    }


def test_both_rule_versions_load():
    version1 = load_rule_version("1.0", ROOT)
    version2 = load_rule_version("2.0", ROOT)

    assert version1["rule_version"] == "1.0"
    assert version2["rule_version"] == "2.0"


@pytest.mark.parametrize(
    "role",
    ["Software Engineer", "Data Analyst"],
)
def test_each_role_has_weights_totalling_100(role):
    rules = load_rule_version("1.0", ROOT)

    criteria = rules["role_rules"][role]["criteria"]

    assert sum(criteria.values()) == 100


def test_software_engineer_scoring_version_1():
    rules = load_rule_version("1.0", ROOT)
    record = make_record(
        "TR0001", "Software Engineer", 90, 80, 100
    )

    result = calculate_score(record, rules)

    # 90*0.40 + 80*0.35 + 100*0.25 = 89
    assert result["score"] == 89.0
    assert result["rule_version"] == "1.0"


def test_data_analyst_uses_its_own_criteria():
    rules = load_rule_version("1.0", ROOT)
    record = make_record(
        "TR0001", "Data Analyst", 90, 80, 100
    )

    result = calculate_score(record, rules)

    # 90*0.35 + 80*0.40 + 100*0.25 = 88.5
    assert result["score"] == 88.5
    assert (
        result["score_breakdown"]["role_relevance_score"]["weight_percent"]
        == 35
    )
    assert (
        result["score_breakdown"]["skill_gap_alignment_score"]["weight_percent"]
        == 40
    )


def test_version_comparison_uses_same_records():
    v1 = load_rule_version("1.0", ROOT)
    v2 = load_rule_version("2.0", ROOT)

    records = [
        make_record("TR0001", "Software Engineer", 90, 80, 100),
        make_record("TR0002", "Data Analyst", 60, 85, 70),
        make_record("TR0003", "Software Engineer", 50, 50, 50),
    ]

    result = compare_rule_versions(records, v1, v2)

    assert result["summary"]["requests_compared"] == 3
    assert len(result["results"]) == 3

    for row in result["results"]:
        assert "score_v1.0" in row
        assert "score_v2.0" in row
        assert "status_v1.0" in row
        assert "status_v2.0" in row
        assert row["routing_changed"] == (
            row["status_v1.0"] != row["status_v2.0"]
        )


def test_identical_versions_cannot_be_compared():
    rules = load_rule_version("1.0", ROOT)

    with pytest.raises(ValueError, match="different versions"):
        compare_rule_versions([], rules, rules)


def test_unknown_employee_role_is_rejected():
    rules = load_rule_version("1.0", ROOT)
    record = make_record(
        "TR0001", "Chief Wizard", 90, 80, 100
    )

    with pytest.raises(ValueError, match="Unsupported employee role"):
        calculate_score(record, rules)


def test_invalid_weights_are_rejected():
    rules = load_rule_version("1.0", ROOT)
    rules["role_rules"]["Software Engineer"]["criteria"][
        "role_relevance_score"
    ] = 41

    with pytest.raises(ValueError, match="100%"):
        validate_rules(rules)


@pytest.mark.parametrize(
    "score, expected",
    [
        (49.99, "NEEDS_JUSTIFICATION_REVIEW"),
        (50, "LND_REVIEW"),
        (74.99, "LND_REVIEW"),
        (75, "MANAGER_APPROVAL_REVIEW"),
        (100, "MANAGER_APPROVAL_REVIEW"),
    ],
)
def test_version_1_software_engineer_thresholds(score, expected):
    rules = load_rule_version("1.0", ROOT)

    assert route_score(
        score,
        rules,
        employee_role="Software Engineer",
    ) == expected


def test_version_2_has_different_manager_threshold():
    rules = load_rule_version("2.0", ROOT)

    assert route_score(
        77,
        rules,
        employee_role="Software Engineer",
    ) == "LND_REVIEW"

    assert route_score(
        78,
        rules,
        employee_role="Software Engineer",
    ) == "MANAGER_APPROVAL_REVIEW"
