
import copy
import pytest

from src.scoring import (
    calculate_score,
    route_score,
    validate_rules,
)


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


def make_request(role=90, gap=80, prerequisites=100):
    return {
        "role_relevance_score": role,
        "skill_gap_alignment_score": gap,
        "prerequisite_completion_score": prerequisites,
    }


def test_score_has_explainable_breakdown():
    result = calculate_score(make_request(), RULES)

    assert result["score"] == 89.0
    assert result["score_breakdown"]["role_relevance_score"] == {
        "input_score": 90.0,
        "weight_percent": 40.0,
        "weighted_contribution": 36.0,
    }
    assert result["score_breakdown"]["skill_gap_alignment_score"][
        "weighted_contribution"
    ] == 28.0
    assert result["rule_version"] == "1.0"


def test_exact_threshold_75_routes_to_manager():
    result = calculate_score(make_request(75, 75, 75), RULES)

    assert result["score"] == 75.0
    assert route_score(result["score"], RULES) == "MANAGER_APPROVAL_REVIEW"


def test_exact_threshold_50_routes_to_lnd():
    result = calculate_score(make_request(50, 50, 50), RULES)

    assert result["score"] == 50.0
    assert route_score(result["score"], RULES) == "LND_REVIEW"


def test_score_below_50_needs_justification_review():
    result = calculate_score(make_request(0, 0, 0), RULES)

    assert result["score"] == 0.0
    assert route_score(result["score"], RULES) == (
        "NEEDS_JUSTIFICATION_REVIEW"
    )


def test_maximum_score():
    result = calculate_score(make_request(100, 100, 100), RULES)

    assert result["score"] == 100.0
    assert route_score(result["score"], RULES) == "MANAGER_APPROVAL_REVIEW"


def test_invalid_input_score_is_rejected():
    with pytest.raises(ValueError):
        calculate_score(make_request(101, 80, 80), RULES)


def test_nonnumeric_input_score_is_rejected():
    with pytest.raises(ValueError):
        calculate_score(make_request("unknown", 80, 80), RULES)


def test_weights_must_total_100():
    bad_rules = copy.deepcopy(RULES)
    bad_rules["criteria"]["role_relevance_score"] = 45

    with pytest.raises(ValueError, match="100%"):
        validate_rules(bad_rules)


def test_thresholds_must_be_ordered():
    bad_rules = copy.deepcopy(RULES)
    bad_rules["thresholds"] = {
        "manager_approval_review": 50,
        "lnd_review": 75,
    }

    with pytest.raises(ValueError, match="greater than"):
        validate_rules(bad_rules)


def test_nonfinite_score_is_rejected():
    with pytest.raises(ValueError):
        calculate_score(make_request(float("nan"), 50, 50), RULES)
