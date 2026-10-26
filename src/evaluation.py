
"""TrainFlow evaluation metrics and workflow-quality diagnostics."""

from __future__ import annotations

from typing import Any
import math

from src.scoring import calculate_score, route_score


def percentage(numerator: int, denominator: int) -> float | None:
    """Return a percentage, or None when the denominator is zero."""

    if denominator == 0:
        return None
    return round(numerator / denominator * 100.0, 2)


def _nonnegative_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def calculate_evaluation_metrics(
    *,
    total_records: int,
    valid_records: int,
    successfully_processed_valid_records: int,
    notification_drafts_generated: int,
    notification_drafts_required: int,
    test_cases_passed: int,
    total_test_cases: int,
    total_workflow_seconds: float,
    valid_records_processed: int,
    applications_reviewed: int,
    applications_requiring_review: int,
    notification_delivery_successes: int = 0,
    notification_dispatch_attempts: int = 0,
) -> dict[str, Any]:
    """Calculate the six required metrics plus simulated delivery success."""

    counts = {
        "total_records": total_records,
        "valid_records": valid_records,
        "successfully_processed_valid_records":
            successfully_processed_valid_records,
        "notification_drafts_generated": notification_drafts_generated,
        "notification_drafts_required": notification_drafts_required,
        "test_cases_passed": test_cases_passed,
        "total_test_cases": total_test_cases,
        "valid_records_processed": valid_records_processed,
        "applications_reviewed": applications_reviewed,
        "applications_requiring_review": applications_requiring_review,
        "notification_delivery_successes": notification_delivery_successes,
        "notification_dispatch_attempts": notification_dispatch_attempts,
    }

    for name, value in counts.items():
        _nonnegative_count(name, value)

    if (
        isinstance(total_workflow_seconds, bool)
        or not isinstance(total_workflow_seconds, (int, float))
        or not math.isfinite(total_workflow_seconds)
        or total_workflow_seconds < 0
    ):
        raise ValueError("total_workflow_seconds must be finite and nonnegative")

    if valid_records > total_records:
        raise ValueError("valid_records cannot exceed total_records")

    if successfully_processed_valid_records > valid_records:
        raise ValueError(
            "Successfully processed records cannot exceed valid records"
        )

    if test_cases_passed > total_test_cases:
        raise ValueError("Passed test cases cannot exceed total test cases")

    if applications_reviewed > applications_requiring_review:
        raise ValueError(
            "Reviewed applications cannot exceed applications requiring review"
        )

    if notification_delivery_successes > notification_dispatch_attempts:
        raise ValueError(
            "Successful deliveries cannot exceed dispatch attempts"
        )

    avg_seconds = (
        total_workflow_seconds / valid_records_processed
        if valid_records_processed
        else None
    )

    metrics = {
        "data_validation_rate_pct": percentage(
            valid_records, total_records
        ),
        "workflow_processing_success_rate_pct": percentage(
            successfully_processed_valid_records, valid_records
        ),
        "notification_draft_generation_rate_pct": percentage(
            notification_drafts_generated,
            notification_drafts_required,
        ),
        "test_pass_rate_pct": percentage(
            test_cases_passed, total_test_cases
        ),
        "average_processing_seconds_per_valid_record": (
            round(avg_seconds, 6) if avg_seconds is not None else None
        ),
        "average_processing_milliseconds_per_valid_record": (
            round(avg_seconds * 1000, 3)
            if avg_seconds is not None else None
        ),
        "human_review_completion_rate_pct": percentage(
            applications_reviewed, applications_requiring_review
        ),
        "simulated_notification_delivery_success_rate_pct": percentage(
            notification_delivery_successes,
            notification_dispatch_attempts,
        ),
    }

    return metrics


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values())
    return str(value)


def summarize_rejections(rejected_records: list[dict]) -> dict[str, int]:
    """Count rejected rows and detect duplicate-related rejection reasons."""

    duplicate_count = 0

    for record in rejected_records:
        combined = " ".join(
            _text(record.get(key, ""))
            for key in (
                "reason",
                "rejection_reason",
                "validation_errors",
                "errors",
                "error",
                "reasons",
            )
        ).casefold()

        explicit_duplicate_flag = bool(
            record.get("is_duplicate", False)
            or record.get("duplicate", False)
        )

        if explicit_duplicate_flag or any(
            word in combined
            for word in ("duplicate", "already exists", "duplicate id")
        ):
            duplicate_count += 1

    return {
        "rejected_or_invalid_records": len(rejected_records),
        "duplicate_rejected_records": duplicate_count,
        "other_rejected_records": len(rejected_records) - duplicate_count,
    }


def check_routing_correctness(
    requests: list[dict],
    rules_by_version: dict[str, dict],
) -> dict[str, Any]:
    """
    Re-check stored proposed routes against their recorded rule versions.

    Requests without enough information are counted as uncheckable instead
    of being silently treated as correct.
    """

    checked = 0
    correct = 0
    mismatches = []
    uncheckable = 0

    for request in requests:
        version = str(request.get("rule_version") or "")
        rules = rules_by_version.get(version)
        proposed_status = request.get("proposed_status")
        score = request.get("score")
        role = request.get("employee_role")

        if (
            rules is None
            or proposed_status is None
            or score is None
            or role is None
        ):
            uncheckable += 1
            continue

        try:
            expected_status = route_score(
                score,
                rules,
                employee_role=role,
            )
        except (ValueError, TypeError):
            uncheckable += 1
            continue

        checked += 1

        if proposed_status == expected_status:
            correct += 1
        else:
            mismatches.append({
                "request_id": request.get("request_id"),
                "rule_version": version,
                "employee_role": role,
                "score": score,
                "stored_status": proposed_status,
                "expected_status": expected_status,
            })

    return {
        "checked_records": checked,
        "correct_routes": correct,
        "incorrect_routes": len(mismatches),
        "routing_correctness_pct": percentage(correct, checked),
        "uncheckable_records": uncheckable,
        "mismatches": mismatches,
    }


def summarize_reviewer_overrides(
    requests: list[dict],
) -> dict[str, Any]:
    """Identify completed human reviews whose outcome differs from routing."""

    overrides = []
    reviewed_count = 0

    for request in requests:
        action = request.get("reviewer_action")
        if not action:
            continue

        reviewed_count += 1
        proposed = request.get("proposed_status")
        revised = request.get("revised_status")
        final_status = request.get("final_status")

        effective_status = (
            revised
            if revised not in (None, "")
            else final_status
        )

        if (
            proposed not in (None, "")
            and effective_status not in (None, "", "PENDING_REVIEW")
            and effective_status != proposed
        ):
            reason = (
                request.get("review_notes")
                or request.get("review_reason")
                or request.get("note")
                or ""
            )

            overrides.append({
                "request_id": request.get("request_id"),
                "proposed_status": proposed,
                "final_status": effective_status,
                "reviewer_action": action,
                "reviewer_id": request.get("reviewer_id"),
                "reason": reason,
                "reason_missing": not bool(str(reason).strip()),
            })

    return {
        "reviewed_count": reviewed_count,
        "override_count": len(overrides),
        "overrides_without_reason": sum(
            item["reason_missing"] for item in overrides
        ),
        "overrides": overrides,
    }


def check_score_consistency(
    requests: list[dict],
    rules: dict,
    repetitions: int = 3,
) -> dict[str, Any]:
    """Recalculate each score repeatedly and compare exact score breakdowns."""

    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise ValueError("repetitions must be an integer")

    if repetitions < 2:
        raise ValueError("repetitions must be at least 2")

    inconsistent = []

    for request in requests:
        outputs = [
            calculate_score(request, rules)
            for _ in range(repetitions)
        ]

        fingerprints = {
            (
                output["score"],
                tuple(
                    (
                        field,
                        values["input_score"],
                        values["weight_percent"],
                        values["weighted_contribution"],
                    )
                    for field, values
                    in sorted(output["score_breakdown"].items())
                ),
                output["rule_version"],
                output["employee_role"],
            )
            for output in outputs
        }

        if len(fingerprints) != 1:
            inconsistent.append(request.get("request_id"))

    return {
        "requests_checked": len(requests),
        "repetitions_per_request": repetitions,
        "inconsistent_score_count": len(inconsistent),
        "consistent_score_count": len(requests) - len(inconsistent),
        "score_consistency_pct": percentage(
            len(requests) - len(inconsistent), len(requests)
        ),
        "inconsistent_request_ids": inconsistent,
    }
