
"""TrainFlow: versioned, role-specific scoring and deterministic routing."""

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


def _validate_criteria(criteria: Any) -> None:
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


def _validate_thresholds(thresholds: Any) -> None:
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")

    if set(thresholds) != set(REQUIRED_THRESHOLDS):
        raise ValueError(
            "thresholds must contain manager_approval_review and lnd_review"
        )

    converted = {}

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

        converted[name] = threshold

    if converted["manager_approval_review"] <= converted["lnd_review"]:
        raise ValueError(
            "Manager review threshold must be greater than L&D threshold"
        )


def validate_rules(rules: dict[str, Any]) -> None:
    """Validate configuration structure, weights, thresholds and policy fields."""

    if not isinstance(rules, dict):
        raise ValueError("Rules must be a JSON object")

    version = rules.get("rule_version")

    if not isinstance(version, str) or not version.strip():
        raise ValueError("rule_version must be a non-empty string")

    roles = rules.get("allowed_roles")

    if (
        not isinstance(roles, list)
        or not roles
        or any(
            not isinstance(role, str)
            or not role.strip()
            or len(role) > 100
            for role in roles
        )
        or len({role.casefold() for role in roles}) != len(roles)
    ):
        raise ValueError(
            "allowed_roles must be a non-empty list of unique role names"
        )

    role_rules = rules.get("role_rules")

    if role_rules is not None:
        if not isinstance(role_rules, dict):
            raise ValueError("role_rules must be an object")

        allowed_lookup = {role.casefold(): role for role in roles}
        configured_lookup = {
            role.casefold(): role for role in role_rules
            if isinstance(role, str)
        }

        if set(configured_lookup) != set(allowed_lookup):
            raise ValueError(
                "role_rules must define exactly the configured allowed_roles"
            )

        for role_name, configuration in role_rules.items():
            if not isinstance(configuration, dict):
                raise ValueError(
                    f"Configuration for {role_name} must be an object"
                )

            if set(configuration) != {"criteria", "thresholds"}:
                raise ValueError(
                    f"{role_name} must define exactly criteria and thresholds"
                )

            _validate_criteria(configuration["criteria"])
            _validate_thresholds(configuration["thresholds"])

    else:
        # Backward compatibility with the original flat rules.json schema.
        _validate_criteria(rules.get("criteria"))
        _validate_thresholds(rules.get("thresholds"))

    actions = rules.get("review_actions")

    if actions is not None:
        expected_actions = {"APPROVE", "EDIT", "HOLD", "REQUEST_INFO"}

        if (
            not isinstance(actions, list)
            or set(actions) != expected_actions
            or len(actions) != len(expected_actions)
        ):
            raise ValueError("review_actions contains invalid actions")

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
    """Load and validate one rules file."""

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



def load_rule_version(version: str, directory=None) -> dict:
    """Load an exact, named rule version."""

    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must be a non-empty string")

    root = (
        Path(directory)
        if directory is not None
        else Path(__file__).resolve().parent.parent
    )

    version_files = {
        "1.0": "rules.json",
        "2.0": "rules_v2.json",
    }

    if version not in version_files:
        raise ValueError(f"Unsupported rule version: {version}")

    path = root / version_files[version]
    rules = load_rules(path)

    if rules["rule_version"] != version:
        raise ValueError(
            f"Requested version {version}, but loaded "
            f"{rules['rule_version']}"
        )

    return rules

def _get_role_configuration(
    rules: dict[str, Any],
    employee_role: str | None = None,
) -> tuple[str, dict, dict]:
    """
    Resolve the criteria and thresholds for one configured role.

    The first allowed role is used only for legacy callers that omit
    employee_role. Production workflow calls must pass the validated role.
    """

    allowed_roles = rules["allowed_roles"]

    if employee_role is None:
        role = allowed_roles[0]
    else:
        if not isinstance(employee_role, str) or not employee_role.strip():
            raise ValueError("employee_role must be a configured role")

        matching = {
            item.casefold(): item
            for item in allowed_roles
        }

        role = matching.get(employee_role.strip().casefold())

        if role is None:
            raise ValueError(
                f"Unsupported employee role: {employee_role}"
            )

    role_rules = rules.get("role_rules")

    if role_rules is None:
        return role, rules["criteria"], rules["thresholds"]

    configuration = next(
        (
            value
            for key, value in role_rules.items()
            if key.casefold() == role.casefold()
        ),
        None,
    )

    if configuration is None:
        raise ValueError(f"No scoring rules configured for {role}")

    return role, configuration["criteria"], configuration["thresholds"]


def calculate_score(
    request: dict[str, Any],
    rules: dict[str, Any],
) -> dict:
    """Calculate a weighted score with an explanation for each criterion."""

    validate_rules(rules)

    if not isinstance(request, dict):
        raise ValueError("request must be an object")

    role, criteria, _ = _get_role_configuration(
        rules,
        request.get("employee_role"),
    )

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

        weight = float(criteria[field])
        contribution = input_score * weight / 100.0
        total += contribution

        breakdown[field] = {
            "input_score": round(input_score, 2),
            "weight_percent": weight,
            "weighted_contribution": round(contribution, 2),
        }

    return {
        "score": round(total, 2),
        "score_breakdown": breakdown,
        "rule_version": rules["rule_version"],
        "employee_role": role,
    }


def route_score(
    score: float,
    rules: dict[str, Any],
    employee_role: str | None = None,
) -> str:
    """Route a numeric score using the selected role's configured thresholds."""

    validate_rules(rules)

    if isinstance(score, bool):
        raise ValueError("Score must be numeric")

    try:
        score = float(score)
    except (TypeError, ValueError) as exc:
        raise ValueError("Score must be numeric") from exc

    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("Score must be between 0 and 100")

    _, _, thresholds = _get_role_configuration(
        rules,
        employee_role,
    )

    if score >= float(thresholds["manager_approval_review"]):
        return "MANAGER_APPROVAL_REVIEW"

    if score >= float(thresholds["lnd_review"]):
        return "LND_REVIEW"

    return "NEEDS_JUSTIFICATION_REVIEW"


def compare_rule_versions(
    requests: list[dict[str, Any]],
    rules_v1: dict[str, Any],
    rules_v2: dict[str, Any],
) -> dict:
    """
    Compare two rule configurations over the same input records.

    Supply already-validated requests, or use the validation module before
    calling this function. No request state is changed and no action is approved.
    """

    validate_rules(rules_v1)
    validate_rules(rules_v2)

    version1 = rules_v1["rule_version"]
    version2 = rules_v2["rule_version"]

    if version1 == version2:
        raise ValueError("The two rules must have different versions")

    rows = []

    for request in requests:
        score1 = calculate_score(request, rules_v1)
        score2 = calculate_score(request, rules_v2)

        status1 = route_score(
            score1["score"],
            rules_v1,
            employee_role=request.get("employee_role"),
        )
        status2 = route_score(
            score2["score"],
            rules_v2,
            employee_role=request.get("employee_role"),
        )

        rows.append({
            "request_id": request.get("request_id"),
            "employee_id": request.get("employee_id"),
            "employee_role": score1["employee_role"],
            f"score_v{version1}": score1["score"],
            f"status_v{version1}": status1,
            f"score_v{version2}": score2["score"],
            f"status_v{version2}": status2,
            "score_delta_v2_minus_v1": round(
                score2["score"] - score1["score"], 2
            ),
            "routing_changed": status1 != status2,
            f"breakdown_v{version1}": score1["score_breakdown"],
            f"breakdown_v{version2}": score2["score_breakdown"],
        })

    total = len(rows)
    changed = sum(row["routing_changed"] for row in rows)

    summary = {
        "rule_version_1": version1,
        "rule_version_2": version2,
        "requests_compared": total,
        "score_changed_count": sum(
            row[f"score_v{version1}"] != row[f"score_v{version2}"]
            for row in rows
        ),
        "routing_changed_count": changed,
        "routing_changed_percentage": (
            round(changed / total * 100, 2) if total else 0.0
        ),
        "mean_score_delta": (
            round(
                sum(row["score_delta_v2_minus_v1"] for row in rows) / total,
                2,
            )
            if total else 0.0
        ),
    }

    return {
        "summary": summary,
        "results": rows,
    }
