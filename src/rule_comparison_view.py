
"""Streamlit UI for comparing TrainFlow scoring-rule versions."""

from pathlib import Path

import pandas as pd
import streamlit as st

from src.scoring import load_rule_version, compare_rule_versions


SCORE_FIELDS = [
    "role_relevance_score",
    "skill_gap_alignment_score",
    "prerequisite_completion_score",
]


def _prepare_comparison_records(source, allowed_roles):
    if isinstance(source, pd.DataFrame):
        frame = source.copy()
    else:
        frame = pd.DataFrame(source)

    if frame.empty:
        return [], {
            "input_rows": 0,
            "eligible_rows": 0,
            "excluded_rows": 0,
            "duplicate_ids": 0,
        }

    if "employee_role" not in frame.columns:
        if "application_role" in frame.columns:
            frame["employee_role"] = frame["application_role"]
        else:
            raise ValueError(
                "Dataset must contain employee_role or application_role."
            )

    if "request_id" not in frame.columns:
        for alternative in ("training_request_id", "application_id", "id"):
            if alternative in frame.columns:
                frame["request_id"] = frame[alternative]
                break

    if "request_id" not in frame.columns:
        raise ValueError("Dataset must contain a request ID column.")

    required = ["request_id", "employee_role", *SCORE_FIELDS]
    missing_columns = [column for column in required if column not in frame]

    if missing_columns:
        raise ValueError(
            "Missing comparison columns: " + ", ".join(missing_columns)
        )

    input_rows = len(frame)

    frame["request_id"] = frame["request_id"].astype("string").str.strip()
    frame["employee_role"] = (
        frame["employee_role"].astype("string").str.strip()
    )

    for field in SCORE_FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")

    frame.replace({pd.NA: None}, inplace=True)

    usable = (
        frame["request_id"].notna()
        & frame["request_id"].ne("")
        & frame["employee_role"].notna()
        & frame["employee_role"].isin(allowed_roles)
    )

    for field in SCORE_FIELDS:
        usable &= (
            frame[field].notna()
            & frame[field].between(0, 100, inclusive="both")
        )

    frame = frame.loc[usable].copy()

    # Keep the first occurrence for comparison and report duplicate IDs.
    duplicate_mask = frame["request_id"].duplicated(keep="first")
    duplicate_ids = int(duplicate_mask.sum())
    frame = frame.loc[~duplicate_mask].copy()

    records = frame.to_dict(orient="records")

    return records, {
        "input_rows": input_rows,
        "eligible_rows": len(records),
        "excluded_rows": input_rows - len(records) - duplicate_ids,
        "duplicate_ids": duplicate_ids,
    }


def render_rule_comparison_tab(records=None):
    """Render the comparison view inside an existing Streamlit tab."""

    st.subheader("Scoring Rule Comparison")
    st.caption(
        "Compare two transparent configurations on the same eligible "
        "training requests. Scores are decision-support indicators, "
        "not automatic approvals."
    )

    try:
        rules_v1 = load_rule_version("1.0")
        rules_v2 = load_rule_version("2.0")
    except (ValueError, FileNotFoundError) as exc:
        st.error(f"Could not load both rule versions: {exc}")
        st.info(
            "Check that rules.json and rules_v2.json are both in "
            "the project root and contain the correct rule_version."
        )
        return

    source_label = "records supplied by the app"

    if records is None:
        default_csv = (
            Path(__file__).resolve().parent.parent
            / "training_requests.csv"
        )
        uploaded = st.file_uploader(
            "Optional: use a different CSV for comparison",
            type=["csv"],
            key="rule_comparison_csv",
        )

        try:
            if uploaded is not None:
                source = pd.read_csv(uploaded)
                source_label = uploaded.name
            elif default_csv.is_file():
                source = pd.read_csv(default_csv)
                source_label = default_csv.name
            else:
                st.warning(
                    "No request records were supplied and "
                    "training_requests.csv was not found."
                )
                return
        except Exception as exc:
            st.error(f"Could not read comparison data: {exc}")
            return
    else:
        source = records

    roles = sorted(set(rules_v1["allowed_roles"]) |
                   set(rules_v2["allowed_roles"]))

    try:
        comparable, preparation = _prepare_comparison_records(
            source, roles
        )
    except (ValueError, TypeError) as exc:
        st.error(f"Comparison data is not usable: {exc}")
        return

    left, right = st.columns(2)

    with left:
        st.markdown("**Rule version 1.0 — Baseline**")
        st.write("File: `rules.json`")
        st.dataframe(
            pd.DataFrame([
                {
                    "Employee role": role,
                    **config["criteria"],
                    **config["thresholds"],
                }
                for role, config in rules_v1["role_rules"].items()
            ]),
            use_container_width=True,
            hide_index=True,
        )

    with right:
        st.markdown("**Rule version 2.0 — Alternative**")
        st.write("File: `rules_v2.json`")
        st.dataframe(
            pd.DataFrame([
                {
                    "Employee role": role,
                    **config["criteria"],
                    **config["thresholds"],
                }
                for role, config in rules_v2["role_rules"].items()
            ]),
            use_container_width=True,
            hide_index=True,
        )

    st.caption(f"Comparison data source: {source_label}")

    m1, m2, m3 = st.columns(3)
    m1.metric("Input rows", preparation["input_rows"])
    m2.metric("Eligible for comparison", preparation["eligible_rows"])
    m3.metric("Duplicate IDs excluded", preparation["duplicate_ids"])

    st.caption(
        f"Rows otherwise excluded due to missing/invalid score inputs, "
        f"missing IDs, or unsupported roles: {preparation['excluded_rows']}."
    )

    if not comparable:
        st.warning("No eligible requests are available for comparison.")
        return

    try:
        comparison = compare_rule_versions(
            comparable, rules_v1, rules_v2
        )
    except (ValueError, TypeError) as exc:
        st.error(f"Comparison failed: {exc}")
        return

    summary = comparison["summary"]

    st.markdown("### Comparison results")
    a, b, c, d = st.columns(4)
    a.metric("Requests compared", summary["requests_compared"])
    b.metric("Scores changed", summary["score_changed_count"])
    c.metric("Routing changed", summary["routing_changed_count"])
    d.metric(
        "Routing change rate",
        f"{summary['routing_changed_percentage']:.2f}%",
    )

    st.metric(
        "Mean score change (2.0 minus 1.0)",
        f"{summary['mean_score_delta']:+.2f}",
    )

    result_frame = pd.DataFrame(comparison["results"])
    visible_columns = [
        column for column in [
            "request_id",
            "employee_id",
            "employee_role",
            "score_v1.0",
            "status_v1.0",
            "score_v2.0",
            "status_v2.0",
            "score_delta_v2_minus_v1",
            "routing_changed",
        ]
        if column in result_frame.columns
    ]

    changed_only = st.checkbox(
        "Show only requests whose routing changes",
        value=False,
        key="rule_comparison_changed_only",
    )

    display_frame = result_frame
    if changed_only:
        display_frame = result_frame[
            result_frame["routing_changed"]
        ]

    st.dataframe(
        display_frame[visible_columns],
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "Download comparison CSV",
        data=result_frame[visible_columns].to_csv(index=False).encode("utf-8"),
        file_name="trainflow_rule_comparison.csv",
        mime="text/csv",
    )

    with st.expander("How to interpret this comparison"):
        st.write(
            "A routing change means the same request was assigned to a "
            "different review category under version 2.0. It does not mean "
            "version 2.0 is more accurate or fair. Evaluate the criteria, "
            "thresholds, actual training outcomes, and review process "
            "before adopting either configuration."
        )
