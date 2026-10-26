
"""TrainFlow evaluation dashboard presentation."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from typing import Any


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------

def _display_value(value: Any, suffix: str = "") -> str:
    """Format metrics with suitable precision for small timing values."""

    if value is None:
        return "N/A"

    if isinstance(value, bool):
        return "Yes" if value else "No"

    if isinstance(value, float):
        if suffix.strip() == "s/record":
            return f"{value:,.6f}{suffix}"
        return f"{value:,.2f}{suffix}"

    if isinstance(value, int):
        return f"{value:,}{suffix}"

    return f"{value}{suffix}"


def _metric(
    column,
    label: str,
    value: Any,
    suffix: str = "",
    help_text: str | None = None,
) -> None:
    """Display one metric."""

    column.metric(
        label=label,
        value=_display_value(value, suffix),
        help=help_text,
    )


def _show_table(
    data: Any,
    *,
    empty_message: str,
    use_container_width: bool = True,
) -> None:
    """Display a list of dictionaries or DataFrame."""

    if data is None:
        st.info(empty_message)
        return

    if isinstance(data, pd.DataFrame):
        frame = data
    else:
        frame = pd.DataFrame(data)

    if frame.empty:
        st.info(empty_message)
        return

    st.dataframe(
        frame,
        use_container_width=use_container_width,
        hide_index=True,
    )


def _section_counts(
    title: str,
    values: dict,
    preferred_keys: list[str] | None = None,
) -> None:
    """Display a dictionary of counts as a compact table."""

    st.markdown(f"#### {title}")

    keys = preferred_keys or list(values.keys())

    rows = [
        {
            "Metric": key.replace("_", " ").strip().title(),
            "Value": values[key],
        }
        for key in keys
        if key in values
    ]

    _show_table(
        rows,
        empty_message="No counts are available.",
    )


# ---------------------------------------------------------------------
# Main evaluation view
# ---------------------------------------------------------------------

def render_evaluation_tab(
    *,
    metrics: dict,
    rejection_summary: dict,
    routing_report: dict,
    override_report: dict,
    consistency_report: dict,
    fairness_report: dict,
    benchmark_report: dict | None = None,
    sla_reminders: list[dict] | None = None,
):
    """
    Render workflow metrics and diagnostics calculated by backend modules.

    The view only presents supplied results. It does not make up evaluation
    counts or claim that the workflow is fair based on similar routing rates.
    """

    metrics = metrics or {}
    rejection_summary = rejection_summary or {}
    routing_report = routing_report or {}
    override_report = override_report or {}
    consistency_report = consistency_report or {}
    fairness_report = fairness_report or {}
    benchmark_report = benchmark_report or {}

    sla_reminders = sla_reminders or []

    st.subheader("Workflow Evaluation")

    st.caption(
        "These metrics measure software and workflow performance. "
        "They do not establish the validity of training decisions or prove "
        "that the scoring criteria are fair."
    )

    # -----------------------------------------------------------------
    # 1. Required evaluation metrics
    # -----------------------------------------------------------------

    st.markdown("### Required Evaluation Metrics")

    st.caption(
        "Percentages use their actual supplied numerator and denominator. "
        "N/A means that the metric could not be calculated from the available "
        "data, such as when its denominator is zero or a test report is missing."
    )

    row1 = st.columns(3)

    _metric(
        row1[0],
        "Data validation rate",
        metrics.get("data_validation_rate_pct"),
        "%",
        "Valid records divided by total submitted records, multiplied by 100.",
    )

    _metric(
        row1[1],
        "Workflow processing success",
        metrics.get("workflow_processing_success_rate_pct"),
        "%",
        "Successfully processed valid records divided by all valid records.",
    )

    _metric(
        row1[2],
        "Notification draft generation",
        metrics.get("notification_draft_generation_rate_pct"),
        "%",
        "Generated drafts divided by drafts required.",
    )

    row2 = st.columns(3)

    _metric(
        row2[0],
        "Test pass rate",
        metrics.get("test_pass_rate_pct"),
        "%",
        "Passed test cases divided by total collected test cases. "
        "Skipped tests are not counted as passed.",
    )

    _metric(
        row2[1],
        "Average processing time",
        metrics.get(
            "average_processing_milliseconds_per_valid_record"
        ),
        " ms/record",
        "Total workflow execution time divided by valid records processed.",
    )

    _metric(
        row2[2],
        "Human review completion",
        metrics.get("human_review_completion_rate_pct"),
        "%",
        "Reviewed applications divided by applications requiring review.",
    )

    st.markdown("#### Processing time in seconds")

    _metric(
        st,
        "Average seconds per valid record",
        metrics.get("average_processing_seconds_per_valid_record"),
        " s/record",
    )

    delivery_rate = metrics.get(
        "simulated_notification_delivery_success_rate_pct"
    )

    st.markdown("#### Additional notification metric")

    _metric(
        st,
        "Simulated notification delivery success",
        delivery_rate,
        "%",
        "Successful simulated deliveries divided by dispatch attempts. "
        "This is not a measurement of real email delivery.",
    )

    st.divider()

    # -----------------------------------------------------------------
    # 2. Data quality and rejected records
    # -----------------------------------------------------------------

    st.markdown("### Data Quality")

    quality_columns = st.columns(3)

    _metric(
        quality_columns[0],
        "Invalid / rejected records",
        rejection_summary.get("rejected_or_invalid_records"),
    )

    _metric(
        quality_columns[1],
        "Duplicate rejected records",
        rejection_summary.get("duplicate_rejected_records"),
    )

    _metric(
        quality_columns[2],
        "Other rejected records",
        rejection_summary.get("other_rejected_records"),
    )

    st.caption(
        "Duplicate counts must come from the import-validation results. "
        "A duplicate file upload and a duplicate request ID within an imported "
        "file are distinct cases and should not be conflated."
    )

    st.divider()

    # -----------------------------------------------------------------
    # 3. Correct routing decisions
    # -----------------------------------------------------------------

    st.markdown("### Routing Correctness")

    routing_columns = st.columns(4)

    _metric(
        routing_columns[0],
        "Records checked",
        routing_report.get("checked_records"),
    )

    _metric(
        routing_columns[1],
        "Correct routing decisions",
        routing_report.get("correct_routes"),
    )

    _metric(
        routing_columns[2],
        "Routing mismatches",
        routing_report.get("incorrect_routes"),
    )

    _metric(
        routing_columns[3],
        "Routing correctness",
        routing_report.get("routing_correctness_pct"),
        "%",
    )

    if routing_report.get("uncheckable_records") is not None:
        st.caption(
            "Uncheckable records: "
            f'{routing_report["uncheckable_records"]}. '
            "These records were not counted as correct."
        )

    st.markdown("#### Routing mismatches")

    _show_table(
        routing_report.get("mismatches", []),
        empty_message="No routing mismatches were reported.",
    )

    st.divider()

    # -----------------------------------------------------------------
    # 4. Human-review overrides
    # -----------------------------------------------------------------

    st.markdown("### Reviewer Overrides")

    override_columns = st.columns(3)

    _metric(
        override_columns[0],
        "Reviewed requests",
        override_report.get("reviewed_count"),
    )

    _metric(
        override_columns[1],
        "Routing overrides",
        override_report.get("override_count"),
    )

    _metric(
        override_columns[2],
        "Overrides without a reason",
        override_report.get("overrides_without_reason"),
    )

    st.caption(
        "An override represents a human-review outcome different from the "
        "system's proposed route. The reason should be documented and auditable."
    )

    overrides = override_report.get("overrides", [])

    if overrides:
        override_frame = pd.DataFrame(overrides)

        if "reason_missing" in override_frame.columns:
            override_frame["reason_missing"] = (
                override_frame["reason_missing"]
                .map({True: "Yes", False: "No"})
            )

        _show_table(
            override_frame,
            empty_message="No overrides are available.",
        )
    else:
        st.info("No reviewer overrides were reported.")

    st.divider()

    # -----------------------------------------------------------------
    # 5. Repeated-execution score consistency
    # -----------------------------------------------------------------

    st.markdown("### Score Consistency")

    consistency_columns = st.columns(4)

    _metric(
        consistency_columns[0],
        "Requests checked",
        consistency_report.get("requests_checked"),
    )

    _metric(
        consistency_columns[1],
        "Repetitions per request",
        consistency_report.get("repetitions_per_request"),
    )

    _metric(
        consistency_columns[2],
        "Consistent scores",
        consistency_report.get("consistent_score_count"),
    )

    _metric(
        consistency_columns[3],
        "Score consistency rate",
        consistency_report.get("score_consistency_pct"),
        "%",
    )

    inconsistent_ids = consistency_report.get(
        "inconsistent_request_ids", []
    )

    if inconsistent_ids:
        st.error(
            f"{len(inconsistent_ids)} request(s) produced inconsistent "
            "scoring results."
        )
        st.dataframe(
            pd.DataFrame({
                "Request ID": inconsistent_ids,
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        count = consistency_report.get("requests_checked", 0)

        if count:
            st.success(
                "No score inconsistencies were detected in the supplied test."
            )
        else:
            st.info("No requests were available for the consistency check.")

    st.divider()

    # -----------------------------------------------------------------
    # 6. Sequential-versus-batch benchmark
    # -----------------------------------------------------------------

    st.markdown("### Sequential vs Batch Benchmark")

    if not benchmark_report:
        st.info(
            "Benchmark results are not available yet. Run the benchmark "
            "on the same validated, unique request records first."
        )
    else:
        benchmark_columns = st.columns(4)

        _metric(
            benchmark_columns[0],
            "Records processed",
            benchmark_report.get("records_processed"),
        )

        _metric(
            benchmark_columns[1],
            "Benchmark repetitions",
            benchmark_report.get("repetitions"),
        )

        _metric(
            benchmark_columns[2],
            "Sequential mean time",
            benchmark_report.get("sequential_mean_seconds"),
            " s",
        )

        _metric(
            benchmark_columns[3],
            "Batch mean time",
            benchmark_report.get("batch_mean_seconds"),
            " s",
        )

        comparison_columns = st.columns(3)

        _metric(
            comparison_columns[0],
            "Sequential milliseconds / record",
            benchmark_report.get("sequential_mean_ms_per_record"),
            " ms",
        )

        _metric(
            comparison_columns[1],
            "Batch milliseconds / record",
            benchmark_report.get("batch_mean_ms_per_record"),
            " ms",
        )

        _metric(
            comparison_columns[2],
            "Batch speedup ratio",
            benchmark_report.get("batch_speedup_ratio"),
            "x",
        )

        equivalent = benchmark_report.get("output_equivalent")

        if equivalent is True:
            st.success(
                "Sequential and batch workflow outputs are equivalent."
            )
        elif equivalent is False:
            st.error(
                "Sequential and batch workflow outputs differ. "
                "Investigate before interpreting the timing results."
            )
        else:
            st.info("Output equivalence was not reported.")

        if benchmark_report.get("timing_note"):
            st.caption(benchmark_report["timing_note"])

        extra_timing = []

        for label, key in [
            ("Sequential median seconds", "sequential_median_seconds"),
            ("Batch median seconds", "batch_median_seconds"),
        ]:
            if benchmark_report.get(key) is not None:
                extra_timing.append({
                    "Measurement": label,
                    "Seconds": benchmark_report[key],
                })

        _show_table(
            extra_timing,
            empty_message="No additional timing statistics are available.",
        )

    st.divider()

    # -----------------------------------------------------------------
    # 7. SLA reminders
    # -----------------------------------------------------------------

    st.markdown("### SLA Reminders")

    reminder_columns = st.columns(3)

    overdue = [
        reminder for reminder in sla_reminders
        if str(reminder.get("reminder_type", "")).upper() == "OVERDUE"
    ]

    due_soon = [
        reminder for reminder in sla_reminders
        if str(reminder.get("reminder_type", "")).upper() == "DUE_SOON"
    ]

    reminder_columns[0].metric(
        "Overdue review requests",
        len(overdue),
    )

    reminder_columns[1].metric(
        "Due soon",
        len(due_soon),
    )

    reminder_columns[2].metric(
        "Total reminders",
        len(sla_reminders),
    )

    if sla_reminders:
        _show_table(
            sla_reminders,
            empty_message="No SLA reminders are available.",
        )
    else:
        st.success(
            "No overdue or due-soon requests were returned by the SLA check."
        )

    st.caption(
        "Reminder counts represent the records returned by the SLA checker. "
        "They do not prove that a reminder notification was sent."
    )

    st.divider()

    # -----------------------------------------------------------------
    # 8. Fairness-oriented scoring review
    # -----------------------------------------------------------------

    st.markdown("### Fairness-Oriented Scoring Review")

    st.warning(
        "Score and routing distributions are diagnostic indicators, not proof "
        "of fairness. These rules require a documented job-related rationale "
        "and validation against appropriate training outcomes."
    )

    if fairness_report.get("rule_version") is not None:
        st.caption(
            f'Rules evaluated: version {fairness_report["rule_version"]}'
        )

    st.markdown("#### Configured scoring criteria and weights")

    _show_table(
        fairness_report.get("criteria", []),
        empty_message="No scoring criteria were supplied for review.",
    )

    st.markdown("#### Scores and routing outcomes by role")

    _show_table(
        fairness_report.get("outcomes_by_role", []),
        empty_message="No role-level outcome summaries are available.",
    )

    st.markdown("#### Criteria requiring review")

    concerns = fairness_report.get("review_concerns", [])

    if concerns:
        for concern in concerns:
            st.warning(concern)
    else:
        st.info(
            "No automated review concerns were supplied. This does not "
            "constitute evidence that the criteria are fair."
        )

    st.markdown("#### Interpretation limitations")

    limitations = fairness_report.get("limitations", [])

    if limitations:
        for limitation in limitations:
            st.write(f"- {limitation}")
    else:
        st.write(
            "The fairness report did not supply explicit limitations. "
            "Do not infer that the scoring system has been validated."
        )

    st.divider()

    # -----------------------------------------------------------------
    # 9. Evaluation interpretation
    # -----------------------------------------------------------------

    st.markdown("### Evaluation Notes")

    st.write(
        "Use these metrics to assess validation quality, workflow reliability, "
        "routing correctness, review completion, notification processing, and "
        "execution performance. Investigate routing mismatches, missing override "
        "reasons, and inconsistent scores before treating the workflow as reliable."
    )

    st.write(
        "The evaluation metrics do not establish that the training suitability "
        "score predicts learning success, that the weights are appropriate for "
        "each role, or that the review process is fair. These questions require "
        "separate validation of the criteria, outcomes, and human-review process."
    )
