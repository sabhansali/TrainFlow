
"""Sequential-versus-batch workflow benchmark."""

from __future__ import annotations

import statistics
import time
from typing import Any

from src.workflow import run_workflow


def _request_signature(request: dict) -> tuple:
    breakdown = request.get("score_breakdown") or {}

    return (
        request.get("request_id"),
        request.get("score"),
        request.get("proposed_status"),
        request.get("rule_version"),
        tuple(
            (
                field,
                values.get("input_score"),
                values.get("weight_percent"),
                values.get("weighted_contribution"),
            )
            for field, values in sorted(breakdown.items())
        ),
    )


def _processed_signatures(result: dict) -> list[tuple]:
    records = result.get("processed_requests", [])
    return sorted(
        (_request_signature(record) for record in records),
        key=lambda row: str(row[0]),
    )


def benchmark_sequential_vs_batch(
    valid_unique_records: list[dict[str, Any]],
    rules: dict,
    repetitions: int = 5,
) -> dict:
    """
    Compare processing one record per call with processing all records at once.

    The input must already be validated and unique. This avoids benchmarking
    different duplicate-detection behaviour between the two execution modes.
    No database state or review decisions are changed by this function.
    """

    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise ValueError("repetitions must be an integer")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if not valid_unique_records:
        raise ValueError("At least one valid record is required")

    request_ids = [
        str(record.get("request_id", "")).strip()
        for record in valid_unique_records
    ]

    if any(not request_id for request_id in request_ids):
        raise ValueError("Every record must have a request_id")
    if len(set(request_ids)) != len(request_ids):
        raise ValueError("Benchmark input must not contain duplicate request IDs")

    for record in valid_unique_records:
        if any(
            record.get(field) is None
            for field in (
                "role_relevance_score",
                "skill_gap_alignment_score",
                "prerequisite_completion_score",
                "employee_role",
            )
        ):
            raise ValueError("Benchmark input contains incomplete scoring fields")

    sequential_times = []
    batch_times = []
    sequential_signature = None
    batch_signature = None

    for _ in range(repetitions):
        start = time.perf_counter()

        sequential_processed = []
        for record in valid_unique_records:
            outcome = run_workflow([record], rules)
            sequential_processed.extend(
                outcome.get("processed_requests", [])
            )

        sequential_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        batch_result = run_workflow(valid_unique_records, rules)
        batch_elapsed = time.perf_counter() - start

        batch_processed = batch_result.get("processed_requests", [])

        if len(sequential_processed) != len(valid_unique_records):
            raise RuntimeError(
                "Sequential run did not process every supplied valid record"
            )
        if len(batch_processed) != len(valid_unique_records):
            raise RuntimeError(
                "Batch run did not process every supplied valid record"
            )

        sequential_result = {"processed_requests": sequential_processed}
        current_sequential_signature = _processed_signatures(
            sequential_result
        )
        current_batch_signature = _processed_signatures(batch_result)

        if sequential_signature is None:
            sequential_signature = current_sequential_signature
            batch_signature = current_batch_signature
        elif (
            sequential_signature != current_sequential_signature
            or batch_signature != current_batch_signature
        ):
            raise RuntimeError(
                "Workflow output changed between benchmark repetitions"
            )

        sequential_times.append(sequential_elapsed)
        batch_times.append(batch_elapsed)

    output_equivalent = sequential_signature == batch_signature
    count = len(valid_unique_records)

    sequential_mean = statistics.mean(sequential_times)
    batch_mean = statistics.mean(batch_times)

    return {
        "records_processed": count,
        "repetitions": repetitions,
        "sequential_mean_seconds": round(sequential_mean, 6),
        "sequential_median_seconds": round(
            statistics.median(sequential_times), 6
        ),
        "sequential_mean_ms_per_record": round(
            sequential_mean / count * 1000, 3
        ),
        "batch_mean_seconds": round(batch_mean, 6),
        "batch_median_seconds": round(
            statistics.median(batch_times), 6
        ),
        "batch_mean_ms_per_record": round(
            batch_mean / count * 1000, 3
        ),
        "batch_speedup_ratio": (
            round(sequential_mean / batch_mean, 3)
            if batch_mean > 0 else None
        ),
        "output_equivalent": output_equivalent,
        "timing_note": (
            "Times are local workflow execution measurements and exclude "
            "external network/email delivery."
        ),
    }
