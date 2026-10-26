"""Sequential-versus-batch workflow benchmark."""

from __future__ import annotations

import statistics
import time
from typing import Any

from src.validation import normalize_record
from src.workflow import run_workflow


from collections.abc import Mapping
from typing import Any


def _request_signature(request: Any) -> tuple:
    """Create a stable signature and validate the processed record shape."""

    if isinstance(request, tuple):
        # Support tuples containing (key, value) pairs.
        try:
            converted = dict(request)
            if converted and all(
                isinstance(key, str) for key in converted
            ):
                request = converted
        except (TypeError, ValueError):
            pass

    if not isinstance(request, Mapping):
        raise TypeError(
            "Benchmark expected each processed request to be a dictionary, "
            f"but received {type(request).__name__}. "
            "Check src/workflow.py: processed_requests."
        )

    breakdown = request.get("score_breakdown") or {}

    if not isinstance(breakdown, Mapping):
        breakdown = {}

    breakdown_signature = []

    for field, values in sorted(breakdown.items()):
        if isinstance(values, Mapping):
            breakdown_signature.append(
                (
                    field,
                    values.get("input_score"),
                    values.get("weight_percent"),
                    values.get("weighted_contribution"),
                )
            )

    return (
        request.get("request_id"),
        request.get("score"),
        request.get("proposed_status"),
        request.get("rule_version"),
        tuple(breakdown_signature),
    )


def _processed_signatures(result: dict) -> list[tuple]:
    """Return processed-request signatures in a stable order."""

    records = result.get("processed_requests", [])

    return sorted(
        (_request_signature(record) for record in records),
        key=lambda row: str(row[0]),
    )


def _prepare_records(
    records: list[dict[str, Any]],
    rules: dict,
) -> tuple[list[dict[str, Any]], int]:
    """
    Normalize records and retain only records accepted by the workflow.

    Database metadata and previously calculated outputs are removed by
    normalize_record() before validation.
    """

    prepared_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    excluded_count = 0

    for original in records:
        if not isinstance(original, dict):
            excluded_count += 1
            continue

        normalized_result = normalize_record(original)

        # Some validation implementations return (normalized_record, errors).
        # Extract the normalized dictionary before accessing its fields.
        if isinstance(normalized_result, tuple):
            if not normalized_result:
                excluded_count += 1
                continue

            record = normalized_result[0]
        else:
            record = normalized_result

        if not isinstance(record, dict):
            excluded_count += 1
            continue

        request_id = str(record.get("request_id") or "").strip().upper()

        if not request_id or request_id in seen_ids:
            excluded_count += 1
            continue

        record["request_id"] = request_id

        # Preflight validation happens outside the timed benchmark.
        result = run_workflow([record], rules)
        processed = result.get("processed_requests", [])

        if len(processed) != 1:
            excluded_count += 1
            continue

        seen_ids.add(request_id)
        prepared_records.append(record)

    return prepared_records, excluded_count


def benchmark_sequential_vs_batch(
    valid_unique_records: list[dict[str, Any]],
    rules: dict,
    repetitions: int = 5,
) -> dict:
    """
    Compare one-record-at-a-time processing with batch processing.

    Only normalized records accepted by run_workflow() are benchmarked.
    Preflight validation is excluded from timing.

    This function does not persist requests, change review decisions,
    modify reviewer permissions, or send notifications.
    """

    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise ValueError("repetitions must be an integer")

    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    if not isinstance(valid_unique_records, list):
        raise ValueError("Benchmark input must be a list of records")

    if not valid_unique_records:
        return {}

    # Normalize and preflight records before measuring execution time.
    records, excluded_count = _prepare_records(
        valid_unique_records,
        rules,
    )

    if not records:
        return {}

    record_count = len(records)

    sequential_times: list[float] = []
    batch_times: list[float] = []

    expected_sequential_signature: list[tuple] | None = None
    expected_batch_signature: list[tuple] | None = None

    for _ in range(repetitions):

        # -----------------------------------------------------------
        # Sequential execution
        # -----------------------------------------------------------

        sequential_processed: list[dict] = []

        start = time.perf_counter()

        for record in records:
            outcome = run_workflow([record], rules)
            processed = outcome.get("processed_requests", [])

            if len(processed) != 1:
                raise RuntimeError(
                    "Sequential benchmark failed after preflight validation. "
                    f"Request ID: {record.get('request_id')!r}. "
                    f"Rejected records: "
                    f"{outcome.get('rejected_records', [])!r}"
                )

            sequential_processed.extend(processed)

        sequential_elapsed = time.perf_counter() - start

        # -----------------------------------------------------------
        # Batch execution
        # -----------------------------------------------------------

        start = time.perf_counter()

        batch_result = run_workflow(records, rules)

        batch_elapsed = time.perf_counter() - start

        batch_processed = batch_result.get("processed_requests", [])

        if len(batch_processed) != record_count:
            raise RuntimeError(
                "Batch benchmark did not process every prevalidated record. "
                f"Expected: {record_count}; "
                f"processed: {len(batch_processed)}. "
                f"Rejected records: "
                f"{batch_result.get('rejected_records', [])!r}"
            )

        # -----------------------------------------------------------
        # Compare output consistency
        # -----------------------------------------------------------

        current_sequential_signature = _processed_signatures({
            "processed_requests": sequential_processed
        })

        current_batch_signature = _processed_signatures(batch_result)

        if expected_sequential_signature is None:
            expected_sequential_signature = current_sequential_signature
            expected_batch_signature = current_batch_signature
        elif (
            expected_sequential_signature != current_sequential_signature
            or expected_batch_signature != current_batch_signature
        ):
            raise RuntimeError(
                "Workflow output changed between benchmark repetitions."
            )

        sequential_times.append(sequential_elapsed)
        batch_times.append(batch_elapsed)

    # ---------------------------------------------------------------
    # Calculate timing metrics
    # ---------------------------------------------------------------

    sequential_mean = statistics.mean(sequential_times)
    batch_mean = statistics.mean(batch_times)

    timing_note = (
        "Measurements cover local workflow execution only. "
        "Preflight validation and external email delivery are excluded. "
        f"{record_count} validated record(s) were benchmarked."
    )

    if excluded_count:
        timing_note += (
            f" {excluded_count} supplied record(s) were excluded because "
            "they were invalid, malformed, or had duplicate request IDs."
        )

    return {
        "records_processed": record_count,
        "repetitions": repetitions,
        "sequential_mean_seconds": round(sequential_mean, 6),
        "sequential_median_seconds": round(
            statistics.median(sequential_times), 6
        ),
        "sequential_mean_ms_per_record": round(
            sequential_mean / record_count * 1000, 3
        ),
        "batch_mean_seconds": round(batch_mean, 6),
        "batch_median_seconds": round(
            statistics.median(batch_times), 6
        ),
        "batch_mean_ms_per_record": round(
            batch_mean / record_count * 1000, 3
        ),
        "batch_speedup_ratio": (
            round(sequential_mean / batch_mean, 3)
            if batch_mean > 0
            else None
        ),
        "output_equivalent": (
            expected_sequential_signature == expected_batch_signature
        ),
        "timing_note": timing_note,
    }