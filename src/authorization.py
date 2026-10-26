
"""Server-side policy checks for TrainFlow request review actions."""

from typing import Any


REVIEW_ACTIONS = {
    "APPROVE",
    "EDIT",
    "HOLD",
    "REQUEST_INFO",
}

ADMIN_ROLES = {"ADMIN", "HR_ADMIN", "LND_ADMIN"}
MANAGER_ROLE = "MANAGER"
LND_REVIEWER_ROLES = {"LND_REVIEWER", "LND_MANAGER"}


def authorize_request_action(
    *,
    identity: dict[str, Any] | None,
    request: dict[str, Any],
    action: str,
) -> None:
    """
    Raise PermissionError unless the database-resolved identity can act.

    Call this using an identity resolved by the authentication/database layer
    and the request fetched inside the same database transaction that applies
    the state update. Never construct identity from Streamlit form values.
    """

    if not identity:
        raise PermissionError("An authenticated active identity is required")

    active_value = identity.get(
        "is_active",
        identity.get("active"),
    )
    status_value = str(identity.get("status", "")).upper()

    if active_value is False or status_value in {
        "INACTIVE",
        "DISABLED",
        "SUSPENDED",
    }:
        raise PermissionError("The account is not active")

    action = str(action).strip().upper()

    if action not in REVIEW_ACTIONS:
        raise PermissionError(f"Unsupported review action: {action}")

    role = str(identity.get("role", "")).strip().upper()

    if role in ADMIN_ROLES:
        return

    if role == MANAGER_ROLE:
        assigned_manager_id = str(
            request.get("manager_id") or ""
        ).strip()

        actor_ids = {
            str(identity.get(key) or "").strip()
            for key in ("user_id", "id", "employee_id", "manager_id")
        }
        actor_ids.discard("")

        if not assigned_manager_id or assigned_manager_id not in actor_ids:
            raise PermissionError(
                "Managers may review only requests assigned to them"
            )

        return

    if role in LND_REVIEWER_ROLES:
        return

    raise PermissionError(
        f"Role {role or '(missing)'} is not authorized to review requests"
    )
