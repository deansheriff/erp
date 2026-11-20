"""Who works which shift on a day (ADR 0019): a roster entry for that day wins (another
pattern, or a day off); otherwise the usual shift applies on its working weekdays."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.attendance.models import RosterEntry, ShiftAssignment, ShiftPattern


@dataclass(frozen=True, slots=True)
class Shift:
    pattern_id: uuid.UUID
    name: str
    start: datetime
    end: datetime
    break_minutes: int
    late_grace_minutes: int
    early_grace_minutes: int


def at(day: date, moment: time) -> datetime:
    """A wall-clock time on a day in the organisation's timezone."""
    return datetime.combine(day, moment, tzinfo=ZoneInfo(get_settings().timezone))


def shift_of(pattern: ShiftPattern, day: date) -> Shift:
    start = at(day, pattern.start_time)
    end = at(day, pattern.end_time)
    if end <= start:  # crosses midnight
        end = at(day + timedelta(days=1), pattern.end_time)
    return Shift(
        pattern.id,
        pattern.name,
        start,
        end,
        pattern.break_minutes,
        pattern.late_grace_minutes,
        pattern.early_grace_minutes,
    )


EN_DASH = chr(0x2013)


def label(pattern: ShiftPattern) -> str:
    return f"{pattern.name} {pattern.start_time:%H:%M}{EN_DASH}{pattern.end_time:%H:%M}"


def shifts(
    db: Session, employee_ids: list[uuid.UUID], start: date, end: date
) -> dict[tuple[uuid.UUID, date], Shift]:
    """Each scheduled (person, day) between two dates. Days without a shift are absent
    from the result."""
    if not employee_ids:
        return {}
    patterns = {p.id: p for p in db.scalars(select(ShiftPattern))}
    usual: dict[uuid.UUID, list[ShiftAssignment]] = defaultdict(list)
    for a in db.scalars(
        select(ShiftAssignment).where(
            ShiftAssignment.employee_id.in_(employee_ids),
            ShiftAssignment.start_date <= end,
            or_(ShiftAssignment.end_date.is_(None), ShiftAssignment.end_date >= start),
        )
    ):
        usual[a.employee_id].append(a)
    roster = {
        (r.employee_id, r.day): r.pattern_id
        for r in db.scalars(
            select(RosterEntry).where(
                RosterEntry.employee_id.in_(employee_ids),
                RosterEntry.day >= start,
                RosterEntry.day <= end,
            )
        )
    }
    found: dict[tuple[uuid.UUID, date], Shift] = {}
    for employee_id in employee_ids:
        day = start
        while day <= end:
            key = (employee_id, day)
            if key in roster:
                pattern_id = roster[key]
            else:
                pattern_id = next(
                    (
                        a.pattern_id
                        for a in usual[employee_id]
                        if a.start_date <= day
                        and (a.end_date is None or a.end_date >= day)
                        and str(day.weekday()) in patterns[a.pattern_id].weekdays
                    ),
                    None,
                )
            if pattern_id is not None:
                found[key] = shift_of(patterns[pattern_id], day)
            day += timedelta(days=1)
    return found


def shift_on(db: Session, employee_id: uuid.UUID, day: date) -> Shift | None:
    return shifts(db, [employee_id], day, day).get((employee_id, day))
