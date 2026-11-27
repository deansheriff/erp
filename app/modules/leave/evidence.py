"""What leave records about a person over a period, as plain facts for the assistant's
appraisal evidence (ADR 0035): approved leave days, never the type of leave (sick leave
is medical). The caller has already checked the person is in the reader's scope."""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.leave.models import LeaveRequest


def facts(db: Session, employee_id: uuid.UUID, start: date, end: date) -> list[tuple[str, str]]:
    requests = list(
        db.scalars(
            select(LeaveRequest).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status == "approved",
                LeaveRequest.start_date <= end,
                LeaveRequest.end_date >= start,
            )
        )
    )
    if not requests:
        return []
    days = sum(float(r.days) for r in requests)
    return [("Leave", f"Approved leave in the period: {days:g} days, in {len(requests)} requests.")]
