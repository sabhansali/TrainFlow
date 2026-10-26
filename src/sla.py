
"""SLA checks for requests waiting for human review."""

from datetime import datetime, timedelta, timezone
from typing import Any


PENDING_REVIEW_STATUSES = {
    "PENDING_REVIEW",
    "MANAGER_APPROVAL_REVIEW",
    "LND_REVIEW",
    "NEEDS_JUSTIFICATION_REVIEW",
}


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    # Store timestamps in UTC. Treat legacy naive ISO timestamps as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def find_sla_reminders(
    requests: list[dict],
    sla_hours: int = 48,
    remind_within_hours: int = 8,
    now: datetime | None = None,
) -> list[dict]:
    """Return due-soon or overdue review reminders, without dispatching them."""

    if isinstance(sla_hours, bool) or not isinstance(sla_hours, int) or sla_hours <= 0:
        raise ValueError("sla_hours must be a positive integer")

    if (
        isinstance(remind_within_hours, bool)
        or not isinstance(remind_within_hours, int)
        or remind_within_hours < 0
    ):
        raise ValueError("remind_within_hours must be a nonnegative integer")

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    results = []

    for request in requests:
        status = str(
            request.get("status") or request.get("final_status") or ""
        ).upper()

        if status not in PENDING_REVIEW_STATUSES:
            continue

        if request.get("reviewer_action"):
            continue

        created_at = _parse_timestamp(
            request.get("submitted_at") or request.get("created_at")
        )

        # Do not infer elapsed time from request_date alone; it may be
        # a date-only imported value rather than an actual submission time.
        if created_at is None:
            continue

        due_at = created_at + timedelta(hours=sla_hours)
        hours_remaining = (due_at - current_time).total_seconds() / 3600

        if hours_remaining < 0:
            reminder_type = "OVERDUE"
        elif hours_remaining <= remind_within_hours:
            reminder_type = "DUE_SOON"
        else:
            continue

        results.append({
            "request_id": request.get("request_id"),
            "employee_id": request.get("employee_id"),
            "employee_role": request.get("employee_role"),
            "status": status,
            "submitted_at": created_at.isoformat(),
            "sla_due_at": due_at.isoformat(),
            "hours_remaining": round(hours_remaining, 2),
            "reminder_type": reminder_type,
        })

    return sorted(
        results,
        key=lambda item: (
            item["reminder_type"] != "OVERDUE",
            item["hours_remaining"],
        ),
    )
