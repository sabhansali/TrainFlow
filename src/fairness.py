
"""Fairness-oriented review of TrainFlow scoring criteria and outcomes."""

from typing import Any

from src.scoring import calculate_score, route_score


def review_scoring_fairness(
    requests: list[dict[str, Any]],
    rules: dict,
) -> dict[str, Any]:
    """Summarize criteria, role-level outcomes, and limitations."""

    criterion_rows = []
    role_rows = []
    concerns = []

    role_rules = rules.get("role_rules")

    if not role_rules:
        role_rules = {
            role: {
                "criteria": rules["criteria"],
                "thresholds": rules["thresholds"],
            }
            for role in rules["allowed_roles"]
        }

    expected_criteria = {
        "role_relevance_score",
        "skill_gap_alignment_score",
        "prerequisite_completion_score",
    }

    for role in rules["allowed_roles"]:
        config = role_rules.get(role)

        if not config:
            concerns.append(
                f"{role}: no scoring configuration was found."
            )
            continue

        criteria = config["criteria"]
        thresholds = config["thresholds"]

        for criterion, weight in criteria.items():
            criterion_rows.append({
                "employee_role": role,
                "criterion": criterion,
                "weight_percent": weight,
            })

        unexpected = set(criteria) - expected_criteria
        if unexpected:
            concerns.append(
                f"{role}: unexpected criteria were configured: "
                + ", ".join(sorted(unexpected))
            )

        if criteria.get("prerequisite_completion_score", 0) > 0:
            concerns.append(
                f"{role}: prerequisite completion affects the score. "
                "Review whether employees have equitable access to the "
                "prerequisite and whether an alternative pathway is needed."
            )

        role_records = [
            record for record in requests
            if str(record.get("employee_role", "")).casefold()
            == role.casefold()
        ]

        scores = []
        routes = {
            "MANAGER_APPROVAL_REVIEW": 0,
            "LND_REVIEW": 0,
            "NEEDS_JUSTIFICATION_REVIEW": 0,
        }

        for record in role_records:
            try:
                evaluation = calculate_score(record, rules)
                route = route_score(
                    evaluation["score"],
                    rules,
                    employee_role=role,
                )
            except (ValueError, TypeError):
                continue

            scores.append(evaluation["score"])
            routes[route] += 1

        scored_count = len(scores)

        role_rows.append({
            "employee_role": role,
            "eligible_requests": len(role_records),
            "scored_requests": scored_count,
            "mean_score": (
                round(sum(scores) / scored_count, 2)
                if scored_count else None
            ),
            "manager_review_rate_pct": (
                round(routes["MANAGER_APPROVAL_REVIEW"] / scored_count * 100, 2)
                if scored_count else None
            ),
            "lnd_review_rate_pct": (
                round(routes["LND_REVIEW"] / scored_count * 100, 2)
                if scored_count else None
            ),
            "justification_review_rate_pct": (
                round(
                    routes["NEEDS_JUSTIFICATION_REVIEW"] / scored_count * 100,
                    2,
                )
                if scored_count else None
            ),
            "manager_threshold": thresholds["manager_approval_review"],
            "lnd_threshold": thresholds["lnd_review"],
        })

    # Different weights/thresholds need documentation and justification.
    if len(role_rules) > 1:
        configurations = list(role_rules.items())
        for index, (role_a, config_a) in enumerate(configurations):
            for role_b, config_b in configurations[index + 1:]:
                if (
                    config_a["criteria"] != config_b["criteria"]
                    or config_a["thresholds"] != config_b["thresholds"]
                ):
                    concerns.append(
                        f"{role_a} and {role_b} use different weights or "
                        "thresholds. Document the job-related rationale and "
                        "review whether the differences are justified."
                    )

    return {
        "rule_version": rules["rule_version"],
        "criteria": criterion_rows,
        "outcomes_by_role": role_rows,
        "review_concerns": list(dict.fromkeys(concerns)),
        "limitations": [
            "Role-level differences are descriptive and do not establish bias.",
            "Similar routing rates do not prove the criteria are fair.",
            "The input contains no validated outcome label proving that a "
            "score predicts training success.",
            "No protected-group fairness analysis is possible from these "
            "role and score fields alone.",
            "Prerequisite completion can reflect access to training or "
            "support, not just individual readiness.",
            "Human reviewers should be able to document, challenge, and "
            "override recommendations with reasons.",
        ],
    }
