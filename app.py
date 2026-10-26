
"""TrainFlow: Streamlit interface for the L&D workflow automation system."""

import json
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

from src.audit import verify_audit_chain
from src.authentication import (
    authenticate_user,
    count_users,
    create_user,
    get_active_identity,
    set_user_active,
)
from src.database import (
    DEFAULT_DB_PATH,
    connect_db,
    initialize_database,
    list_rejected_records,
    list_requests,
)
from src.notifications import (
    approve_notification_draft,
    dispatch_notification,
    generate_notification_draft,
)
from src.scoring import calculate_score, load_rules, load_rule_version
from src.evaluation import (
    calculate_evaluation_metrics,
    check_routing_correctness,
    check_score_consistency,
    summarize_rejections,
    summarize_reviewer_overrides,
)
from src.evaluation_view import render_evaluation_tab
from src.fairness import review_scoring_fairness
from src.benchmark import benchmark_sequential_vs_batch
from src.sla import find_sla_reminders
from src.rule_comparison_view import render_rule_comparison_tab
from src.workflow import (
    apply_review_action,
    get_import_batches_for_user,
    get_rejected_records_for_user,
    process_import_file,
    reopen_request,
)


# ---------------------------------------------------------------------
# Configuration and initialization
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="TrainFlow | Employee Learning & Development",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(DEFAULT_DB_PATH)
RULE_FILES = {
    "1.0": APP_ROOT / "rules.json",
    "2.0": APP_ROOT / "rules_v2.json",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Timing is session-scoped: it measures actual import runs in this browser
# session, without introducing writes into the workflow database.
st.session_state.setdefault(
    "trainflow_import_timing",
    {"total_seconds": 0.0, "valid_records_processed": 0},
)

initialize_database(DB_PATH)


# ---------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------

def show_error(operation, error):
    """Display the error and its traceback for debugging."""

    st.error(f"{operation} failed: {error}")
    st.exception(error)


def can_access_request(request, role):
    """UI visibility filter. Backend actions perform their own checks."""

    if role == "ADMIN":
        return True

    target = (
        request.get("revised_status")
        or request.get("proposed_status")
    )

    if role == "MANAGER":
        return target == "MANAGER_APPROVAL_REVIEW"

    if role == "LND_REVIEWER":
        return target in {
            "LND_REVIEW",
            "NEEDS_JUSTIFICATION_REVIEW",
        }

    return False


def get_visible_requests(identity):
    all_requests = list_requests(DB_PATH, limit=10000)

    return [
        request for request in all_requests
        if can_access_request(request, identity["role"])
    ]


def summary_frame(requests):
    columns = [
        "request_id",
        "employee_id",
        "employee_name",
        "employee_role",
        "training_course",
        "score",
        "rule_version",
        "proposed_status",
        "final_status",
        "notification_status",
    ]

    rows = [{key: request.get(key) for key in columns}
            for request in requests]

    return pd.DataFrame(rows, columns=columns)


def display_score_breakdown(request):
    breakdown = request.get("score_breakdown", {})

    if not breakdown:
        st.info("No scoring breakdown is available.")
        return

    rows = []

    for criterion, values in breakdown.items():
        rows.append({
            "Criterion": criterion,
            "Input Score": values["input_score"],
            "Weight (%)": values["weight_percent"],
            "Weighted Contribution": values["weighted_contribution"],
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    st.metric(
        "Training suitability score",
        f'{request["score"]:.2f}/100',
    )

    st.caption(f'Rule version: {request["rule_version"]}')


def get_visible_drafts(identity, visible_requests):
    """Return notification drafts belonging to requests the user can access."""

    visible_ids = {request["request_id"] for request in visible_requests}

    with closing(connect_db(DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT notification_id, request_id, subject, message,
                   status, attempts, created_at, approved_by, last_error
            FROM notification_drafts
            ORDER BY created_at DESC
            LIMIT 1000
            """
        ).fetchall()

    return [
        dict(row) for row in rows
        if row["request_id"] in visible_ids
    ]


def get_recent_audit_events(limit=100):
    with closing(connect_db(DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT event_id, timestamp, event_type, actor_id,
                   request_id, details_json, prev_hash, event_hash
            FROM audit_events
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    events = []

    for row in rows:
        event = dict(row)

        try:
            event["details"] = json.loads(event.pop("details_json"))
        except json.JSONDecodeError:
            event["details"] = "[Invalid event JSON]"
            event.pop("details_json", None)

        events.append(event)

    return events


# ---------------------------------------------------------------------
# Evaluation helpers (read-only except for browser-session timing)
# ---------------------------------------------------------------------

def _coerce_count(value):
    """Return a non-negative integer for a numeric count, otherwise None."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _extract_count(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            count = _coerce_count(mapping[key])
            if count is not None:
                return count
    return None


def _parse_test_report(uploaded_file):
    """Parse a JUnit XML or simple JSON test report without executing tests."""

    if uploaded_file is None:
        return 0, 0, None

    raw = uploaded_file.getvalue()
    suffix = Path(uploaded_file.name).suffix.lower()

    try:
        if suffix == ".xml":
            root = ET.fromstring(raw)
            cases = list(root.iter("testcase"))
            total = len(cases)
            passed = 0
            for case in cases:
                child_names = {child.tag.rsplit("}", 1)[-1] for child in list(case)}
                if not child_names.intersection({"failure", "error", "skipped"}):
                    passed += 1
            return passed, total, None

        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON test report must contain an object.")

        # Support either a top-level summary or pytest-style {"summary": {...}}.
        summary = payload.get("summary", payload)
        if not isinstance(summary, dict):
            raise ValueError("The JSON report has no usable summary object.")

        passed = _extract_count(summary, ("passed", "tests_passed", "pass_count"))
        failed = _extract_count(summary, ("failed", "failures", "tests_failed", "fail_count"))
        errors = _extract_count(summary, ("errors", "error_count")) or 0
        skipped = _extract_count(summary, ("skipped", "tests_skipped", "skip_count")) or 0
        total = _extract_count(summary, ("total", "tests", "total_tests", "test_count"))

        if passed is None:
            raise ValueError("JSON report must include a 'passed' count.")
        if total is None:
            if failed is None:
                raise ValueError("JSON report must include 'total' or a 'failed' count.")
            total = passed + failed + errors + skipped

        if passed > total:
            raise ValueError("The passed count cannot exceed the total count.")
        return passed, total, None

    except (ET.ParseError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return 0, 0, str(exc)


def _admin_database_evaluation_counts():
    """Read aggregate counters directly; this function performs no writes."""

    with closing(connect_db(DB_PATH)) as connection:
        batch = connection.execute(
            """
            SELECT COUNT(*) AS batch_count,
                   COALESCE(SUM(total_records), 0) AS total_records,
                   COALESCE(SUM(valid_records), 0) AS valid_records,
                   COALESCE(SUM(inserted_records + skipped_records), 0) AS processed_valid_records
            FROM import_batches
            """
        ).fetchone()

        review = connection.execute(
            """
            SELECT COUNT(*) AS required,
                   SUM(CASE WHEN reviewer_action IS NOT NULL
                             AND reviewer_action != '' THEN 1 ELSE 0 END) AS reviewed
            FROM training_requests
            WHERE proposed_status IN (
                'MANAGER_APPROVAL_REVIEW', 'LND_REVIEW',
                'NEEDS_JUSTIFICATION_REVIEW'
            )
            """
        ).fetchone()

        drafts = connection.execute(
            """
            SELECT COUNT(DISTINCT nd.request_id) AS generated
            FROM notification_drafts AS nd
            JOIN training_requests AS tr ON tr.request_id = nd.request_id
            WHERE tr.final_status IN (
                'APPROVED', 'REQUIRES_REVIEW', 'ON_HOLD', 'AWAITING_INFORMATION'
            )
            """
        ).fetchone()

        required_drafts = connection.execute(
            """
            SELECT COUNT(*) AS required
            FROM training_requests
            WHERE final_status IN (
                'APPROVED', 'REQUIRES_REVIEW', 'ON_HOLD', 'AWAITING_INFORMATION'
            )
            """
        ).fetchone()

        delivery = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   SUM(CASE WHEN result = 'SENT' THEN 1 ELSE 0 END) AS successes
            FROM notification_attempts
            """
        ).fetchone()

    return {
        "batch_count": int(batch["batch_count"] or 0),
        "total_records": int(batch["total_records"] or 0),
        "valid_records": int(batch["valid_records"] or 0),
        "processed_valid_records": int(batch["processed_valid_records"] or 0),
        "applications_requiring_review": int(review["required"] or 0),
        "applications_reviewed": int(review["reviewed"] or 0),
        "notification_drafts_generated": int(drafts["generated"] or 0),
        "notification_drafts_required": int(required_drafts["required"] or 0),
        "notification_dispatch_attempts": int(delivery["attempts"] or 0),
        "notification_delivery_successes": int(delivery["successes"] or 0),
    }


def _is_scored_for_rules(request, rules):
    try:
        calculate_score(request, rules)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _requests_for_version(requests, version):
    return [
        request for request in requests
        if str(request.get("rule_version", "")) == str(version)
    ]


def _prepare_benchmark_records(requests, rules):
    prepared = []
    seen_ids = set()
    for request in requests:
        request_id = request.get("request_id")
        if not request_id or request_id in seen_ids:
            continue
        if _is_scored_for_rules(request, rules):
            prepared.append(request)
            seen_ids.add(request_id)
    return prepared


# ---------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------

def login_screen():
    st.title("TrainFlow")
    st.subheader("Employee Learning & Development Management")
    st.write(
        "Automated employee training nomination, review, "
        "and approval workflow."
    )

    if count_users(DB_PATH) == 0:
        st.warning(
            "No user accounts exist yet. Close this page and initialize "
            "your first administrator from the project terminal:"
        )
        st.code("python -m scripts.bootstrap_admin", language="bash")
        st.stop()

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        try:
            identity = authenticate_user(DB_PATH, username, password)

            if identity is None:
                st.error("Invalid username or password.")
            else:
                # Store the user ID only; reload the role from SQLite
                # on every rerun and before every protected backend action.
                st.session_state["trainflow_user_id"] = identity["user_id"]
                st.rerun()

        except Exception:
            st.error("Unable to authenticate. Check the database and try again.")

    st.stop()


user_id = st.session_state.get("trainflow_user_id")
identity = get_active_identity(DB_PATH, user_id) if user_id else None

if identity is None:
    st.session_state.pop("trainflow_user_id", None)
    login_screen()


# ---------------------------------------------------------------------
# Authenticated application shell
# ---------------------------------------------------------------------

role = identity["role"]

with st.sidebar:
    st.title("TrainFlow")
    st.caption("Employee Learning & Development")
    st.write(f"Signed in as **{identity['username']}**")
    st.caption(f"Role: {role}")

    if st.button("Sign out", use_container_width=True):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

    st.divider()
    st.caption("Notifications are simulated; no real email is sent.")


st.title("TrainFlow Dashboard")
st.caption(
    "Explainable training evaluation, authorized human review, "
    "and auditable workflow automation."
)


# ---------------------------------------------------------------------
# Restricted roles receive aggregate-only information
# ---------------------------------------------------------------------

if role in {"EMPLOYEE", "READ_ONLY"}:
    st.info(
        "This account has aggregate-only access in the current prototype. "
        "Individual request access will be enabled only after an employee "
        "identity mapping and corresponding authorization policy are configured."
    )

    all_requests = list_requests(DB_PATH, limit=10000)

    col1, col2, col3 = st.columns(3)
    col1.metric("Stored training requests", len(all_requests))
    col2.metric(
        "Pending review",
        sum(r["final_status"] in {
            "PENDING_REVIEW", "REQUIRES_REVIEW"
        } for r in all_requests),
    )
    col3.metric(
        "Approved requests",
        sum(r["final_status"] == "APPROVED" for r in all_requests),
    )

    st.stop()


# HR roles can see only the requests permitted by the UI role filter.
visible_requests = get_visible_requests(identity)
visible_ids = {request["request_id"] for request in visible_requests}


# ---------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------

tab_names = [
    "Overview",
    "Import Requests",
    "Review Queue",
    "Notifications",
    "Data Quality",
    "Audit & Security",
]

if role == "ADMIN":
    tab_names.append("User Management")
    tab_names.append("Rule Comparison")
    tab_names.append("Evaluation & Fairness")

tabs = st.tabs(tab_names)
tab_lookup = dict(zip(tab_names, tabs))


# ---------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------

with tab_lookup["Overview"]:
    st.subheader("Workflow Overview")

    total = len(visible_requests)
    pending = sum(
        request["final_status"] in {
            "PENDING_REVIEW", "REQUIRES_REVIEW"
        }
        for request in visible_requests
    )
    approved = sum(
        request["final_status"] == "APPROVED"
        for request in visible_requests
    )
    held = sum(
        request["final_status"] in {
            "ON_HOLD", "AWAITING_INFORMATION"
        }
        for request in visible_requests
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Accessible requests", total)
    c2.metric("Awaiting review", pending)
    c3.metric("Approved", approved)
    c4.metric("On hold / information", held)

    st.subheader("Proposed workflow status")

    counts = {}
    for request in visible_requests:
        status = request["proposed_status"]
        counts[status] = counts.get(status, 0) + 1

    if counts:
        st.dataframe(
            pd.DataFrame([
                {"Proposed status": status, "Requests": count}
                for status, count in sorted(counts.items())
            ]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No requests are currently available to this account.")

    st.subheader("Training requests")
    st.dataframe(
        summary_frame(visible_requests),
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Scores are deterministic rule-based assessments of synthetic "
        "training requests. They are not automatic final decisions."
    )


# ---------------------------------------------------------------------
# Import requests
# ---------------------------------------------------------------------

with tab_lookup["Import Requests"]:
    st.subheader("Import Employee Training Requests")

    if role not in {"ADMIN", "LND_REVIEWER"}:
        st.warning("Only administrators and L&D reviewers can import data.")
    else:
        st.write(
            "Upload a CSV or JSON file. The system validates its schema, "
            "quarantines invalid records, scores valid requests, and stores "
            "the import batch and audit event."
        )

        uploaded = st.file_uploader(
            "Choose training request CSV or JSON",
            type=["csv", "json"],
            accept_multiple_files=False,
            help="Maximum file size: 10 MB.",
        )

        if uploaded is not None:
            uploaded_bytes = uploaded.getvalue()
            st.write(f"Selected file: `{Path(uploaded.name).name}`")
            st.write(f"File size: {len(uploaded_bytes):,} bytes")

            if len(uploaded_bytes) > MAX_UPLOAD_BYTES:
                st.error("The uploaded file exceeds the 10 MB size limit.")
            else:
                selected_rule_version = st.selectbox(
                    "Scoring rule version",
                    options=["1.0", "2.0"],
                    format_func=lambda version: (
                        f"Version {version} — "
                        + ("Baseline" if version == "1.0" else "Alternative")
                    ),
                    key="import_rule_version",
                )
                selected_rules_path = RULE_FILES[selected_rule_version]

                try:
                    selected_rules = load_rules(selected_rules_path)
                    rules_valid = True
                    st.caption(
                        f"Validated scoring configuration: version "
                        f"{selected_rule_version}"
                    )
                    with st.expander("Preview selected scoring configuration"):
                        st.json({
                            "rule_version": selected_rules.get("rule_version"),
                            "role_rules": selected_rules.get(
                                "role_rules",
                                {
                                    "Default": {
                                        "criteria": selected_rules.get("criteria"),
                                        "thresholds": selected_rules.get("thresholds"),
                                    }
                                },
                            ),
                        })
                except (ValueError, FileNotFoundError, OSError) as exc:
                    rules_valid = False
                    st.error(f"Cannot use the selected scoring rules: {exc}")

                if st.button(
                    "Validate and process import",
                    type="primary",
                    disabled=not rules_valid,
                ):
                    original_name = Path(uploaded.name).name
                    started_at = time.perf_counter()

                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="trainflow_import_"
                        ) as temp_directory:
                            temporary_path = Path(temp_directory) / original_name
                            temporary_path.write_bytes(uploaded_bytes)

                            result = process_import_file(
                                file_path=temporary_path,
                                db_path=DB_PATH,
                                actor_user_id=identity["user_id"],
                                rules_path=selected_rules_path,
                            )

                        elapsed = time.perf_counter() - started_at
                        summary = result.get("summary", {})
                        persistence = result.get("persistence", {})
                        st.success(
                            f"Import processing completed using rule version "
                            f"{selected_rule_version}."
                        )
                        st.json(summary)

                        if persistence.get("duplicate_import"):
                            st.warning(
                                "This exact file content has already been imported. "
                                "No duplicate batch was created."
                            )
                        if persistence.get("skipped_request_ids"):
                            st.warning(
                                "Some request IDs already existed in the database. "
                                "Existing records were not overwritten."
                            )

                        # Timing data stays in session state and is not used to alter decisions.
                        if not persistence.get("duplicate_import"):
                            processed_count = _extract_count(
                                persistence,
                                ("valid_records", "inserted_records"),
                            )
                            if processed_count is None:
                                processed_count = _extract_count(
                                    summary,
                                    (
                                        "valid_records",
                                        "processed_valid_records",
                                        "valid_count",
                                        "accepted_records",
                                    ),
                                )
                            if processed_count is None:
                                processed_count = 0
                            timing = st.session_state["trainflow_import_timing"]
                            timing["total_seconds"] += elapsed
                            timing["valid_records_processed"] += processed_count
                            st.session_state["trainflow_import_timing"] = timing

                    except Exception as exc:
                        show_error("Import", exc)


# ---------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------

with tab_lookup["Review Queue"]:
    st.subheader("Human Review Queue")

    reviewable = [
        request for request in visible_requests
        if request["final_status"] in {
            "PENDING_REVIEW", "REQUIRES_REVIEW"
        }
    ]

    if reviewable:
        options = {
            (
                f'{request["request_id"]} | '
                f'{request["training_course"]} | '
                f'Score {request["score"]:.2f} | '
                f'{request["proposed_status"]}'
            ): request
            for request in reviewable
        }

        selected_label = st.selectbox(
            "Select a request",
            options=list(options.keys()),
            key="review_request_select",
        )
        selected = options[selected_label]

        with st.expander("Request details and score explanation", expanded=True):
            st.write(f'**Request ID:** {selected["request_id"]}')
            st.write(f'**Employee:** {selected["employee_name"]}')
            st.write(f'**Employee role:** {selected["employee_role"]}')
            st.write(f'**Course:** {selected["training_course"]}')
            st.write(f'**Proposed status:** {selected["proposed_status"]}')
            st.write(f'**Final status:** {selected["final_status"]}')
            st.write(f'**Justification:** {selected["justification"]}')
            display_score_breakdown(selected)

        with st.form("review_action_form"):
            action = st.selectbox(
                "Review action",
                ["APPROVE", "EDIT", "HOLD", "REQUEST_INFO"],
            )
            notes = st.text_area(
                "Review notes / documented reason",
                max_chars=2000,
            )
            override_status = st.selectbox(
                "Revised review destination (used only for EDIT)",
                [
                    "MANAGER_APPROVAL_REVIEW",
                    "LND_REVIEW",
                    "NEEDS_JUSTIFICATION_REVIEW",
                ],
            )
            submitted = st.form_submit_button(
                "Submit review action",
                type="primary",
            )

        if submitted:
            try:
                updated = apply_review_action(
                    db_path=DB_PATH,
                    request_id=selected["request_id"],
                    actor_user_id=identity["user_id"],
                    action=action,
                    notes=notes,
                    override_status=(
                        override_status if action == "EDIT" else None
                    ),
                )

                st.success(
                    f'Request {updated["request_id"]} updated to '
                    f'{updated["final_status"]}.'
                )
                st.rerun()

            except Exception as exc:
                show_error("Review action", exc)

    else:
        st.info("No requests are waiting in your review queues.")

    st.divider()
    st.subheader("Held requests and requests awaiting information")

    reopenable = [
        request for request in visible_requests
        if request["final_status"] in {
            "ON_HOLD", "AWAITING_INFORMATION"
        }
    ]

    if reopenable:
        choices = {
            f'{r["request_id"]} | {r["training_course"]} | {r["final_status"]}': r
            for r in reopenable
        }
        reopen_label = st.selectbox(
            "Select a request to reopen",
            list(choices.keys()),
            key="reopen_request_select",
        )
        reopen_selected = choices[reopen_label]

        with st.form("reopen_request_form"):
            reopen_notes = st.text_area(
                "Reason for reopening",
                max_chars=2000,
            )
            reopen_submitted = st.form_submit_button("Reopen for review")

        if reopen_submitted:
            try:
                updated = reopen_request(
                    db_path=DB_PATH,
                    request_id=reopen_selected["request_id"],
                    actor_user_id=identity["user_id"],
                    notes=reopen_notes,
                )
                st.success(
                    f'Request reopened: {updated["request_id"]}.'
                )
                st.rerun()
            except Exception as exc:
                show_error("Reopen request", exc)
    else:
        st.caption("No held requests are visible to this account.")


# ---------------------------------------------------------------------
# Notification drafts and simulated delivery
# ---------------------------------------------------------------------

with tab_lookup["Notifications"]:
    st.subheader("Notification Drafts")

    st.warning(
        "Delivery is simulated. No email service is connected. "
        "Every dispatch must follow explicit message approval."
    )

    if visible_requests:
        request_options = {
            (
                f'{request["request_id"]} | '
                f'{request["training_course"]} | '
                f'{request["final_status"]}'
            ): request
            for request in visible_requests
        }

        with st.form("generate_draft_form"):
            draft_request_label = st.selectbox(
                "Choose a training request",
                list(request_options.keys()),
            )
            create_draft_submitted = st.form_submit_button(
                "Generate notification draft"
            )

        if create_draft_submitted:
            try:
                request = request_options[draft_request_label]
                draft = generate_notification_draft(
                    DB_PATH,
                    request["request_id"],
                    identity["user_id"],
                )
                st.success(
                    f'Draft ready: {draft["notification_id"]}'
                )
                st.rerun()
            except Exception as exc:
                show_error("Draft generation", exc)

    drafts = get_visible_drafts(identity, visible_requests)

    if drafts:
        st.subheader("Existing messages")

        draft_labels = {
            (
                f'{draft["notification_id"][:8]} | '
                f'{draft["request_id"]} | '
                f'{draft["status"]} | '
                f'Attempts: {draft["attempts"]}'
            ): draft
            for draft in drafts
        }

        selected_draft_label = st.selectbox(
            "Select a notification",
            list(draft_labels.keys()),
        )
        selected_draft = draft_labels[selected_draft_label]

        st.write(f'**Subject:** {selected_draft["subject"]}')
        st.text_area(
            "Message preview",
            value=selected_draft["message"],
            height=150,
            disabled=True,
            key=f'preview_{selected_draft["notification_id"]}',
        )
        st.write(f'**Status:** {selected_draft["status"]}')
        st.write(
            f'**Delivery attempts:** {selected_draft["attempts"]} / 3'
        )

        if selected_draft["last_error"] if "last_error" in selected_draft else False:
            st.error(selected_draft["last_error"])

        notification_id = selected_draft["notification_id"]

        if selected_draft["status"] == "DRAFT":
            if st.button("Approve this exact message", type="primary"):
                try:
                    approve_notification_draft(
                        DB_PATH,
                        notification_id,
                        identity["user_id"],
                    )
                    st.success("Message approved for simulated dispatch.")
                    st.rerun()
                except Exception as exc:
                    show_error("Message approval", exc)

        elif (
            selected_draft["status"] in {"APPROVED", "FAILED"}
            and selected_draft["attempts"] < 3
        ):
            simulate_failure = st.checkbox(
                "Simulate a delivery failure",
                value=False,
                key=f'fail_{notification_id}',
                help="Use this to test bounded retries and failure logging.",
            )

            if st.button("Run one simulated delivery attempt"):
                try:
                    result = dispatch_notification(
                        DB_PATH,
                        notification_id,
                        identity["user_id"],
                        simulate_failure=simulate_failure,
                    )
                    st.success(
                        f'Simulated result: {result["status"]}; '
                        f'attempt {result["attempts"]} of 3.'
                    )
                    st.rerun()
                except Exception as exc:
                    show_error("Notification dispatch", exc)

        elif selected_draft["status"] == "SENT":
            st.success("Simulation completed successfully. No real email was sent.")

        else:
            st.info("This notification has no further permitted delivery attempts.")

    else:
        st.info("No notification drafts are available for your accessible requests.")


# ---------------------------------------------------------------------
# Import history and data quality
# ---------------------------------------------------------------------

with tab_lookup["Data Quality"]:
    st.subheader("Import History and Quarantined Records")

    if role not in {"ADMIN", "LND_REVIEWER"}:
        st.warning("You do not have permission to view quarantined employee records.")
    else:
        try:
            batches = get_import_batches_for_user(
                DB_PATH,
                identity["user_id"],
                limit=200,
            )
            if batches:
                st.dataframe(
                    pd.DataFrame(batches),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No import batches have been recorded.")

            rejected = get_rejected_records_for_user(
                DB_PATH,
                identity["user_id"],
                limit=1000,
            )

            st.subheader(f"Rejected records: {len(rejected)}")

            if rejected:
                rejected_rows = []
                for item in rejected:
                    rejected_rows.append({
                        "rejection_id": item["rejection_id"],
                        "batch_id": item["batch_id"],
                        "source_row": item["source_row"],
                        "request_id": item["request_id"],
                        "reason": item["reason"],
                    })

                st.dataframe(
                    pd.DataFrame(rejected_rows),
                    use_container_width=True,
                    hide_index=True,
                )

                selected_rejection = st.selectbox(
                    "Inspect a quarantined record",
                    options=[
                        item["rejection_id"] for item in rejected
                    ],
                )
                detail = next(
                    item for item in rejected
                    if item["rejection_id"] == selected_rejection
                )
                st.write("**Validation reason**")
                st.write(detail["reason"])
                st.write("**Quarantined record**")
                st.json(detail["record"])

        except Exception as exc:
            show_error("Data quality view", exc)


# ---------------------------------------------------------------------
# Audit chain and security
# ---------------------------------------------------------------------

with tab_lookup["Audit & Security"]:
    st.subheader("Audit Integrity")

    if role != "ADMIN":
        st.info(
            "Full audit-event inspection is restricted to administrators. "
            "Your workflow operations are still recorded by the backend."
        )
    else:
        if st.button("Verify audit hash chain", type="primary"):
            try:
                result = verify_audit_chain(DB_PATH)

                if result["valid"]:
                    st.success(
                        f'Chain verified: {result["events_checked"]} events checked.'
                    )
                else:
                    st.error(
                        f'Integrity verification failed at event '
                        f'{result["first_invalid_event_id"]}: {result["reason"]}'
                    )

                st.json(result)

            except Exception as exc:
                show_error("Audit verification", exc)

        st.subheader("Recent audit events")
        events = get_recent_audit_events(limit=100)

        if events:
            st.dataframe(
                pd.DataFrame(events),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No audit events have been recorded.")


# ---------------------------------------------------------------------
# Administrator-only user management
# ---------------------------------------------------------------------

if role == "ADMIN":
    with tab_lookup["User Management"]:
        st.subheader("Create User Account")

        st.caption(
            "User creation is administrator-only. Passwords are hashed "
            "before storage; never use real employee credentials for testing."
        )

        with st.form("create_user_form"):
            new_username = st.text_input("Username")
            new_password = st.text_input(
                "Temporary password",
                type="password",
            )
            confirm_password = st.text_input(
                "Confirm temporary password",
                type="password",
            )
            new_role = st.selectbox(
                "Assigned role",
                [
                    "MANAGER",
                    "LND_REVIEWER",
                    "EMPLOYEE",
                    "READ_ONLY",
                    "ADMIN",
                ],
            )

            create_user_submitted = st.form_submit_button(
                "Create account",
                type="primary",
            )

        if create_user_submitted:
            if new_password != confirm_password:
                st.error("The passwords do not match.")
            else:
                try:
                    created = create_user(
                        DB_PATH,
                        actor_user_id=identity["user_id"],
                        username=new_username,
                        password=new_password,
                        role=new_role,
                    )
                    st.success(
                        f'Created {created["username"]} '
                        f'with role {created["role"]}.'
                    )
                    st.caption(f'User ID: {created["user_id"]}')
                    st.rerun()
                except Exception as exc:
                    show_error("User creation", exc)

        st.divider()
        st.subheader("Manage accounts")

        with closing(connect_db(DB_PATH)) as connection:
            user_rows = connection.execute(
                """
                SELECT user_id, username, role, is_active,
                       created_at, last_login_at
                FROM users ORDER BY username
                """
            ).fetchall()

        user_list = [dict(row) for row in user_rows]

        if user_list:
            st.dataframe(
                pd.DataFrame(user_list),
                use_container_width=True,
                hide_index=True,
            )

            targets = [
                item for item in user_list
                if item["user_id"] != identity["user_id"]
            ]

            if targets:
                target_options = {
                    (
                        f'{item["username"]} | {item["role"]} | '
                        f'{"ACTIVE" if item["is_active"] else "INACTIVE"}'
                    ): item
                    for item in targets
                }

                with st.form("account_status_form"):
                    target_label = st.selectbox(
                        "Select an account",
                        list(target_options.keys()),
                    )
                    target_active = st.selectbox(
                        "Account status",
                        ["ACTIVE", "INACTIVE"],
                    )
                    status_submitted = st.form_submit_button(
                        "Update account status"
                    )

                if status_submitted:
                    try:
                        target = target_options[target_label]

                        set_user_active(
                            DB_PATH,
                            actor_user_id=identity["user_id"],
                            target_user_id=target["user_id"],
                            is_active=(target_active == "ACTIVE"),
                        )
                        st.success("Account status updated.")
                        st.rerun()
                    except Exception as exc:
                        show_error("Account status update", exc)
        else:
            st.info("No user accounts exist.")

# ---------------------------------------------------------------------
# Administrator-only rule comparison
# ---------------------------------------------------------------------

if role == "ADMIN":
    with tab_lookup["Rule Comparison"]:
        all_requests_for_admin = list_requests(DB_PATH, limit=10000)
        render_rule_comparison_tab(records=all_requests_for_admin)


# ---------------------------------------------------------------------
# Administrator-only evaluation and fairness diagnostics
# ---------------------------------------------------------------------

if role == "ADMIN":
    with tab_lookup["Evaluation & Fairness"]:
        st.subheader("Workflow Evaluation & Fairness Diagnostics")
        st.caption(
            "Read-only diagnostics over persisted workflow records. "
            "The benchmark does not write requests, change decisions, or send notifications."
        )

        all_requests_for_eval = list_requests(DB_PATH, limit=10000)
        try:
            # This aggregate dashboard is ADMIN-only, so include rejected rows
            # across all import batches rather than only this user's batches.
            rejected_for_eval = list_rejected_records(DB_PATH, limit=10000)
        except Exception as exc:
            rejected_for_eval = []
            st.warning(f"Could not load quarantined records for evaluation: {exc}")

        aggregate_counts = _admin_database_evaluation_counts()

        st.markdown("#### Test execution report")
        st.write(
            "Upload a JUnit XML report or a JSON report with test counts. "
            "TrainFlow does not execute the test suite from the dashboard."
        )
        test_report_file = st.file_uploader(
            "Optional pytest/JUnit or JSON test report",
            type=["xml", "json"],
            key="evaluation_test_report",
        )
        test_cases_passed, total_test_cases, test_report_error = _parse_test_report(
            test_report_file
        )
        if test_report_error:
            st.error(f"Could not read test report: {test_report_error}")
        elif test_report_file is not None:
            st.success(
                f"Test report loaded: {test_cases_passed} passed out of "
                f"{total_test_cases} total test cases."
            )
        else:
            st.info("Test pass rate is N/A until a test report is uploaded.")

        timing = st.session_state.get(
            "trainflow_import_timing",
            {"total_seconds": 0.0, "valid_records_processed": 0},
        )
        metrics = calculate_evaluation_metrics(
            total_records=aggregate_counts["total_records"],
            valid_records=aggregate_counts["valid_records"],
            successfully_processed_valid_records=aggregate_counts[
                "processed_valid_records"
            ],
            notification_drafts_generated=aggregate_counts[
                "notification_drafts_generated"
            ],
            notification_drafts_required=aggregate_counts[
                "notification_drafts_required"
            ],
            test_cases_passed=test_cases_passed,
            total_test_cases=total_test_cases,
            total_workflow_seconds=float(timing.get("total_seconds", 0.0)),
            valid_records_processed=int(timing.get("valid_records_processed", 0)),
            applications_reviewed=aggregate_counts["applications_reviewed"],
            applications_requiring_review=aggregate_counts[
                "applications_requiring_review"
            ],
            notification_delivery_successes=aggregate_counts[
                "notification_delivery_successes"
            ],
            notification_dispatch_attempts=aggregate_counts[
                "notification_dispatch_attempts"
            ],
        )

        rejection_summary = summarize_rejections(rejected_for_eval)
        override_report = summarize_reviewer_overrides(all_requests_for_eval)

        try:
            rules_by_version = {
                version: load_rule_version(version, directory=APP_ROOT)
                for version in RULE_FILES
            }
        except (ValueError, FileNotFoundError, OSError) as exc:
            rules_by_version = {}
            st.error(f"Could not load one or more rule versions: {exc}")

        routing_report = check_routing_correctness(
            all_requests_for_eval,
            rules_by_version,
        )

        diagnostic_version = st.selectbox(
            "Rule version for score consistency, fairness, and benchmark checks",
            options=["1.0", "2.0"],
            key="evaluation_rule_version",
        )
        diagnostic_rules = rules_by_version.get(diagnostic_version)
        version_records = _requests_for_version(
            all_requests_for_eval, diagnostic_version
        )

        if diagnostic_rules is not None:
            try:
                consistency_report = check_score_consistency(
                    version_records,
                    diagnostic_rules,
                    repetitions=3,
                )
            except Exception as exc:
                consistency_report = {
                    "records_checked": 0,
                    "consistent": False,
                    "error": str(exc),
                }

            try:
                fairness_report = review_scoring_fairness(
                    version_records,
                    diagnostic_rules,
                )
            except Exception as exc:
                fairness_report = {
                    "criteria": [],
                    "role_outcomes": [],
                    "concerns": [f"Fairness analysis could not be completed: {exc}"],
                    "limitations": [
                        "This diagnostic is descriptive and does not establish fairness or absence of bias."
                    ],
                }

            if st.button(
                "Run read-only sequential vs batch benchmark",
                type="secondary",
                key="run_workflow_benchmark",
            ):
                eligible_records = _prepare_benchmark_records(
                    version_records,
                    diagnostic_rules,
                )
                if not eligible_records:
                    st.warning(
                        "No compatible, uniquely identified requests are available "
                        "for this rule version's benchmark."
                    )
                else:
                    try:
                        benchmark_report = benchmark_sequential_vs_batch(
                            eligible_records,
                            diagnostic_rules,
                            repetitions=5,
                        )
                        st.session_state[
                            f"trainflow_benchmark_{diagnostic_version}"
                        ] = benchmark_report
                        st.success(
                            "Benchmark completed. No workflow records or decisions were changed."
                        )
                    except Exception as exc:
                        show_error("Read-only benchmark", exc)

            benchmark_report = st.session_state.get(
                f"trainflow_benchmark_{diagnostic_version}"
            )
            try:
                sla_hours = int(diagnostic_rules.get("sla_hours", 48))
            except (TypeError, ValueError):
                sla_hours = 48
            try:
                sla_reminders = find_sla_reminders(
                    all_requests_for_eval,
                    sla_hours=sla_hours,
                )
            except Exception as exc:
                sla_reminders = []
                st.warning(f"SLA diagnostics could not be calculated: {exc}")
        else:
            consistency_report = {
                "records_checked": 0,
                "consistent": False,
                "error": "Selected rule configuration could not be loaded.",
            }
            fairness_report = {
                "criteria": [],
                "role_outcomes": [],
                "concerns": ["Selected rule configuration could not be loaded."],
                "limitations": [],
            }
            benchmark_report = None
            sla_reminders = []
        metrics = dict(metrics or {})

# Preserve timing measured during an actual import, when available.
timing_metrics = st.session_state.get(
    "workflow_timing_metrics", {}
)
metrics.update(timing_metrics)

# If import timing is unavailable, use the completed read-only benchmark.
# These values measure run_workflow() execution, not database persistence.
if benchmark_report:
    records_processed = benchmark_report.get("records_processed")
    batch_seconds = benchmark_report.get("batch_mean_seconds")

    if (
        isinstance(records_processed, int)
        and not isinstance(records_processed, bool)
        and records_processed > 0
        and isinstance(batch_seconds, (int, float))
        and batch_seconds >= 0
    ):
        average_seconds = batch_seconds / records_processed

        if metrics.get(
            "average_processing_seconds_per_valid_record"
        ) is None:
            metrics[
                "average_processing_seconds_per_valid_record"
            ] = average_seconds

        if metrics.get(
            "average_processing_milliseconds_per_valid_record"
        ) is None:
            metrics[
                "average_processing_milliseconds_per_valid_record"
            ] = average_seconds * 1000

        metrics.update(timing_metrics)

        render_evaluation_tab(
            metrics=metrics,
            rejection_summary=rejection_summary,
            routing_report=routing_report,
            override_report=override_report,
            consistency_report=consistency_report,
            fairness_report=fairness_report,
            benchmark_report=benchmark_report,
            sla_reminders=sla_reminders,
        )
