"""What attendance records about a person over a period, as plain facts for the
assistant's appraisal evidence (ADR 0035): days clocked in and days late. The caller has
already checked the person is in the reader's scope."""

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.attendance.models import AttendanceRecord


def facts(db: Session, employee_id: uuid.UUID, start: date, end: date) -> list[tuple[str, str]]:
    records = list(
        db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.work_date >= start,
                AttendanceRecord.work_date <= end,
                AttendanceRecord.clock_in_at.is_not(None),
            )
        )
    )
    if not records:
        return []
    late = sum(
        1
        for r in records
        if r.scheduled_start is not None
        and r.clock_in_at > r.scheduled_start + timedelta(minutes=r.late_grace_minutes)
    )
    return [
        ("Attendance", f"Days they clocked in: {len(records)}."),
        ("Attendance", f"Days they clocked in after their shift's start and grace period: {late}."),
    ]
