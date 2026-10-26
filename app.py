
"""TrainFlow: Streamlit interface for the L&D workflow automation system."""

import json
import tempfile
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
    list_requests,
)
from src.notifications import (
    approve_notification_draft,
    dispatch_notification,
    generate_notification_draft,
)
from src.scoring import load_rules
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
RULES_PATH = APP_ROOT / "rules.json"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

initialize_database(DB_PATH)


# ---------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------

def show_error(operation, error):
    """Show a concise UI error without displaying a traceback."""

    st.error(f"{operation} failed: {error}")


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
                if st.button("Validate and process import", type="primary"):
                    original_name = Path(uploaded.name).name

                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="trainflow_import_"
                        ) as temp_directory:
                            # Use the sanitized original filename for import history.
                            temporary_path = (
                                Path(temp_directory) / original_name
                            )
                            temporary_path.write_bytes(uploaded_bytes)

                            result = process_import_file(
                                file_path=temporary_path,
                                db_path=DB_PATH,
                                actor_user_id=identity["user_id"],
                                rules_path=RULES_PATH,
                            )

                        summary = result["summary"]
                        st.success("Import processing completed.")
                        st.json(summary)

                        if result["persistence"]["duplicate_import"]:
                            st.warning(
                                "This exact file content has already been "
                                "imported. No duplicate batch was created."
                            )

                        if result.get("persistence", {}).get(
                            "skipped_request_ids"
                        ):
                            st.warning(
                                "Some request IDs already existed in the "
                                "database. Existing records were not overwritten."
                            )

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
