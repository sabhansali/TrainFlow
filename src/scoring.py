
"""TrainFlow: configurable scoring and deterministic workflow routing."""

import json
import math
from pathlib import Path
from typing import Any


CRITERIA_FIELDS = [
    "role_relevance_score",
    "skill_gap_alignment_score",
    "prerequisite_completion_score",
]

REQUIRED_THRESHOLDS = [
    "manager_approval_review",
    "lnd_review",
]

ROUTING_STATUSES = {
    "MANAGER_APPROVAL_REVIEW",
    "LND_REVIEW",
    "NEEDS_JUSTIFICATION_REVIEW",
}


def validate_rules(rules: dict[str, Any]) -> None:
    """Raise ValueError if the scoring configuration is unsafe or invalid."""

    if not isinstance(rules, dict):
        raise ValueError("Rules must be a JSON object")

    version = rules.get("rule_version")

    if not isinstance(version, str) or not version.strip():
        raise ValueError("rule_version must be a non-empty string")

    criteria = rules.get("criteria")

    if not isinstance(criteria, dict):
        raise ValueError("criteria must be an object")

    if set(criteria) != set(CRITERIA_FIELDS):
        raise ValueError(
            "criteria must contain exactly: "
            + ", ".join(CRITERIA_FIELDS)
        )

    weights = []

    for field in CRITERIA_FIELDS:
        value = criteria[field]

        if isinstance(value, bool):
            raise ValueError(f"Weight for {field} must be numeric")

        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Weight for {field} must be numeric"
            ) from exc

        if not math.isfinite(weight) or weight < 0:
            raise ValueError(
                f"Weight for {field} must be finite and nonnegative"
            )

        weights.append(weight)

    if not math.isclose(sum(weights), 100.0, abs_tol=1e-9):
        raise ValueError("Scoring weights must add up to exactly 100%")

    thresholds = rules.get("thresholds")

    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")

    if set(thresholds) != set(REQUIRED_THRESHOLDS):
        raise ValueError(
            "thresholds must contain manager_approval_review and lnd_review"
        )

    converted_thresholds = {}

    for name in REQUIRED_THRESHOLDS:
        value = thresholds[name]

        if isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")

        try:
            threshold = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc

        if not math.isfinite(threshold) or not 0 <= threshold <= 100:
            raise ValueError(f"{name} must be between 0 and 100")

        converted_thresholds[name] = threshold

    if (
        converted_thresholds["manager_approval_review"]
        <= converted_thresholds["lnd_review"]
    ):
        raise ValueError(
            "Manager review threshold must be greater than L&D threshold"
        )

    roles = rules.get("allowed_roles")

    if (
        not isinstance(roles, list)
        or not roles
        or any(not isinstance(role, str) or not role.strip() for role in roles)
        or len({role.casefold() for role in roles}) != len(roles)
    ):
        raise ValueError(
            "allowed_roles must be a non-empty list of unique role names"
        )

    sla_hours = rules.get("sla_hours", 48)

    if (
        isinstance(sla_hours, bool)
        or not isinstance(sla_hours, int)
        or sla_hours <= 0
    ):
        raise ValueError("sla_hours must be a positive integer")

    retries = rules.get("notification_max_retries", 2)

    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or not 0 <= retries <= 2
    ):
        raise ValueError(
            "notification_max_retries must be an integer between 0 and 2"
        )


def load_rules(file_path: str | Path | None = None) -> dict:
    """Load and validate rules.json."""

    if file_path is None:
        file_path = Path(__file__).resolve().parent.parent / "rules.json"

    path = Path(file_path)

    if not path.is_file():
        raise FileNotFoundError(f"Rules file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            rules = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed rules JSON: {exc}") from exc

    validate_rules(rules)
    return rules


def calculate_score(
    request: dict[str, Any],
    rules: dict[str, Any],
) -> dict:
    """
    Calculate a reproducible training-suitability score.

    The returned breakdown explains the input score, weight, and
    weighted contribution for every criterion.
    """

    validate_rules(rules)

    breakdown = {}
    total = 0.0

    for field in CRITERIA_FIELDS:
        value = request.get(field)

        if isinstance(value, bool):
            raise ValueError(f"{field} must be numeric")

        try:
            input_score = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric") from exc

        if not math.isfinite(input_score) or not 0 <= input_score <= 100:
            raise ValueError(f"{field} must be between 0 and 100")

        weight = float(rules["criteria"][field])
        contribution = input_score * weight / 100.0
        total += contribution

        breakdown[field] = {
            "input_score": round(input_score, 2),
            "weight_percent": weight,
            "weighted_contribution": round(contribution, 2),
        }

    final_score = round(total, 2)

    return {
        "score": final_score,
        "score_breakdown": breakdown,
        "rule_version": rules["rule_version"],
    }


def route_score(
    score: float,
    rules: dict[str, Any],
) -> str:
    """Route a valid score to a review category."""

    validate_rules(rules)

    if isinstance(score, bool):
        raise ValueError("Score must be numeric")

    try:
        score = float(score)
    except (TypeError, ValueError) as exc:
        raise ValueError("Score must be numeric") from exc

    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("Score must be between 0 and 100")

    thresholds = rules["thresholds"]

    if score >= float(thresholds["manager_approval_review"]):
        return "MANAGER_APPROVAL_REVIEW"

    if score >= float(thresholds["lnd_review"]):
        return "LND_REVIEW"

    return "NEEDS_JUSTIFICATION_REVIEW"
