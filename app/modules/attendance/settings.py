"""Attendance settings and scheduling (ADR 0019): shift patterns, branch locations,
people's usual shifts and single-day roster changes."""

import uuid
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.attendance import schedule
from app.modules.attendance.models import (
    AttendanceLocation,
    RosterEntry,
    ShiftAssignment,
    ShiftPattern,
)
from app.modules.hr import service as people

SETTINGS = "attendance.settings.manage"
SCHEDULE = "attendance.schedule.manage"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_RADIUS_M = 150


def _require(
    db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None
) -> None:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    if not engine.check(db, actor.user_id, permission, resource):
        raise PermissionDenied()


def _int(value: str, field: str, low: int, high: int, message: str) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        raise ValidationFailed(field_errors={field: message}) from None
    if not low <= number <= high:
        raise ValidationFailed(field_errors={field: message})
    return number


def _time(value: str, field: str) -> time:
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ValidationFailed(field_errors={field: "Enter a time such as 08:00."}) from None


# ------------------------------------------------------------------ shift patterns


def list_patterns(db: Session, *, active_only: bool = False) -> list[ShiftPattern]:
    stmt = select(ShiftPattern).order_by(ShiftPattern.start_time, ShiftPattern.name)
    if active_only:
        stmt = stmt.where(ShiftPattern.is_active)
    return list(db.scalars(stmt))


def _pattern_values(pattern: ShiftPattern) -> dict[str, object]:
    return {
        "name": pattern.name,
        "start": pattern.start_time.isoformat("minutes"),
        "end": pattern.end_time.isoformat("minutes"),
        "break_minutes": pattern.break_minutes,
        "weekdays": pattern.weekdays,
        "late_grace_minutes": pattern.late_grace_minutes,
        "early_grace_minutes": pattern.early_grace_minutes,
        "active": pattern.is_active,
    }


def save_pattern(
    db: Session,
    actor: Actor,
    pattern_id: uuid.UUID | None,
    *,
    name: str,
    start_time: str,
    end_time: str,
    break_minutes: str,
    weekdays: list[str],
    late_grace: str,
    early_grace: str,
    is_active: bool,
) -> uuid.UUID:
    _require(db, actor, SETTINGS)
    name = " ".join(name.split())
    if not name:
        raise ValidationFailed(field_errors={"name": "Give the shift a name."})
    start, end = _time(start_time, "start_time"), _time(end_time, "end_time")
    if start == end:
        raise ValidationFailed(field_errors={"end_time": "The shift must end at another time."})
    days = "".join(sorted({d for d in weekdays if d in "0123456" and len(d) == 1}))
    if not days:
        raise ValidationFailed(field_errors={"weekdays": "Choose the days this shift is worked."})
    values = {
        "break_minutes": _int(break_minutes, "break_minutes", 0, 600, "Enter 0 to 600 minutes."),
        "late_grace_minutes": _int(late_grace, "late_grace", 0, 240, "Enter 0 to 240 minutes."),
        "early_grace_minutes": _int(early_grace, "early_grace", 0, 240, "Enter 0 to 240 minutes."),
    }
    clash = db.scalar(
        select(ShiftPattern.id).where(
            func.lower(ShiftPattern.name) == name.lower(),
            *([ShiftPattern.id != pattern_id] if pattern_id else []),
        )
    )
    if clash:
        raise ValidationFailed(field_errors={"name": "Another shift has this name."})
    if pattern_id is None:
        pattern = ShiftPattern(name=name, start_time=start, end_time=end, weekdays=days, **values)
        db.add(pattern)
        db.flush()
        record_by(
            db,
            actor,
            "attendance.pattern.create",
            entity_type="shift_pattern",
            entity_id=str(pattern.id),
            changes=diff_changes({}, _pattern_values(pattern)),
        )
        return pattern.id
    found = db.get(ShiftPattern, pattern_id)
    if found is None:
        raise NotFound()
    pattern = found
    before = _pattern_values(pattern)
    pattern.name, pattern.start_time, pattern.end_time, pattern.weekdays = name, start, end, days
    pattern.break_minutes = values["break_minutes"]
    pattern.late_grace_minutes = values["late_grace_minutes"]
    pattern.early_grace_minutes = values["early_grace_minutes"]
    pattern.is_active = is_active
    record_by(
        db,
        actor,
        "attendance.pattern.update",
        entity_type="shift_pattern",
        entity_id=str(pattern.id),
        changes=diff_changes(before, _pattern_values(pattern)),
    )
    return pattern.id


# ------------------------------------------------------------------ branch locations


@dataclass(frozen=True, slots=True)
class LocationRow:
    branch_id: uuid.UUID
    branch: str
    rules: AttendanceLocation | None
    people: int


def locations(db: Session) -> list[LocationRow]:
    rules = {r.branch_id: r for r in db.scalars(select(AttendanceLocation))}
    ids = people.current_ids(db)
    counts: dict[uuid.UUID, int] = {}
    for branch_id in people.branches_of(db, ids).values():
        if branch_id is not None:
            counts[branch_id] = counts.get(branch_id, 0) + 1
    return [
        LocationRow(branch_id, name, rules.get(branch_id), counts.get(branch_id, 0))
        for branch_id, name in people.branch_choices(db)
    ]


def _degrees(value: str, field: str, limit: int) -> Decimal | None:
    if not value.strip():
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        number = Decimal(limit + 1)
    if not number.is_finite() or abs(number) > limit:
        raise ValidationFailed(
            field_errors={field: f"Enter a number between -{limit} and {limit}."}
        )
    return round(number, 6)


def _location_values(row: AttendanceLocation) -> dict[str, object]:
    return {
        "geofence": row.geofence_enabled,
        "latitude": str(row.latitude) if row.latitude is not None else None,
        "longitude": str(row.longitude) if row.longitude is not None else None,
        "radius_m": row.radius_m,
        "photo": row.require_photo,
    }


def save_location(
    db: Session,
    actor: Actor,
    branch_id: uuid.UUID,
    *,
    geofence_enabled: bool,
    latitude: str,
    longitude: str,
    radius_m: str,
    require_photo: bool,
) -> None:
    _require(db, actor, SETTINGS)
    if branch_id not in dict(people.branch_choices(db)):
        raise NotFound()
    lat = _degrees(latitude, "latitude", 90)
    lng = _degrees(longitude, "longitude", 180)
    if (lat is None) != (lng is None):
        raise ValidationFailed(field_errors={"longitude": "Enter both latitude and longitude."})
    if geofence_enabled and lat is None:
        raise ValidationFailed(
            field_errors={"latitude": "Enter the branch's location to turn the geofence on."}
        )
    radius = _int(
        radius_m or str(DEFAULT_RADIUS_M), "radius_m", 20, 5000, "Enter 20 to 5,000 metres."
    )
    row = db.scalars(
        select(AttendanceLocation).where(AttendanceLocation.branch_id == branch_id)
    ).one_or_none()
    before = _location_values(row) if row else {}
    if row is None:
        row = AttendanceLocation(branch_id=branch_id)
        db.add(row)
    row.geofence_enabled, row.latitude, row.longitude = geofence_enabled, lat, lng
    row.radius_m, row.require_photo = radius, require_photo
    db.flush()
    record_by(
        db,
        actor,
        "attendance.location.update",
        entity_type="branch",
        entity_id=str(branch_id),
        changes=diff_changes(before, _location_values(row)),
    )


# ------------------------------------------------------------------ usual shifts


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    id: uuid.UUID
    employee_id: uuid.UUID
    name: str
    pattern: str
    start_date: date
    end_date: date | None


def assignments(db: Session, actor: Actor, *, q: str = "") -> list[AssignmentRow]:
    """Current and future usual shifts for people in scope."""
    rows, _ = people.page_in_scope(db, actor, SCHEDULE, q=q, size=500)
    names = {r.id: r.full_name for r in rows}
    patterns = {p.id: schedule.label(p) for p in db.scalars(select(ShiftPattern))}
    found = db.scalars(
        select(ShiftAssignment)
        .where(
            ShiftAssignment.employee_id.in_(list(names)),
            or_(ShiftAssignment.end_date.is_(None), ShiftAssignment.end_date >= clock.today()),
        )
        .order_by(ShiftAssignment.start_date)
    )
    result = [
        AssignmentRow(
            a.id,
            a.employee_id,
            names[a.employee_id],
            patterns[a.pattern_id],
            a.start_date,
            a.end_date,
        )
        for a in found
    ]
    return sorted(result, key=lambda a: (a.name, a.start_date))


def assign(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID | None,
    *,
    pattern_id: uuid.UUID | None,
    start: date | None,
    end: date | None,
) -> uuid.UUID:
    """Give someone a usual shift from a date. A shift they already have that started
    earlier ends the day before; one starting on or after this date must be changed
    first."""
    if employee_id is None:
        raise ValidationFailed(field_errors={"employee_id": "Choose a person."})
    _require(db, actor, SCHEDULE, employee_id)
    facts = people.status_and_account(db, employee_id)
    if facts is None:
        raise NotFound()
    if facts[0] == "terminated":
        raise Conflict("This person has left the organisation.")
    pattern = db.get(ShiftPattern, pattern_id) if pattern_id else None
    if pattern is None or not pattern.is_active:
        raise ValidationFailed(field_errors={"pattern_id": "Choose a shift."})
    if start is None:
        raise ValidationFailed(field_errors={"start_date": "Choose the first day."})
    if end is not None and end < start:
        raise ValidationFailed(field_errors={"end_date": "The end can't be before the start."})
    overlapping = list(
        db.scalars(
            select(ShiftAssignment)
            .where(
                ShiftAssignment.employee_id == employee_id,
                or_(ShiftAssignment.end_date.is_(None), ShiftAssignment.end_date >= start),
                *([ShiftAssignment.start_date <= end] if end else []),
            )
            .with_for_update()
        )
    )
    if any(a.start_date >= start for a in overlapping):
        raise Conflict("They already have a shift starting on or after this date. End it first.")
    for previous in overlapping:
        previous.end_date = start - timedelta(days=1)
    row = ShiftAssignment(
        employee_id=employee_id,
        pattern_id=pattern.id,
        start_date=start,
        end_date=end,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "attendance.assignment.create",
        entity_type="shift_assignment",
        entity_id=str(row.id),
        changes=diff_changes(
            {},
            {
                "employee_id": str(employee_id),
                "shift": pattern.name,
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
            },
        ),
    )
    return row.id


def end_assignment(
    db: Session, actor: Actor, assignment_id: uuid.UUID, *, last_day: date | None
) -> None:
    row = db.get(ShiftAssignment, assignment_id)
    if row is None:
        raise NotFound()
    _require(db, actor, SCHEDULE, row.employee_id)
    last_day = last_day or clock.today()
    if last_day < row.start_date:
        db.delete(row)  # never started
        action = "attendance.assignment.delete"
        changes = None
    else:
        changes = {
            "end": {
                "from": row.end_date.isoformat() if row.end_date else None,
                "to": last_day.isoformat(),
            }
        }
        row.end_date = last_day
        action = "attendance.assignment.end"
    record_by(
        db,
        actor,
        action,
        entity_type="shift_assignment",
        entity_id=str(assignment_id),
        changes=changes,
    )


# ------------------------------------------------------------------ roster


@dataclass(frozen=True, slots=True)
class RosterCell:
    day: date
    label: str  # the shift's name, or "Off"
    kind: str  # usual, changed, off, none


@dataclass(frozen=True, slots=True)
class RosterRow:
    employee_id: uuid.UUID
    name: str
    cells: list[RosterCell]


def roster_week(db: Session, actor: Actor, monday: date, *, q: str = "") -> list[RosterRow]:
    """The week for people in scope who have a usual shift or a roster change in it."""
    rows, _ = people.page_in_scope(db, actor, SCHEDULE, q=q, size=500)
    ids = [r.id for r in rows]
    sunday = monday + timedelta(days=6)
    shifts = schedule.shifts(db, ids, monday, sunday)
    changed = {
        (r.employee_id, r.day)
        for r in db.scalars(
            select(RosterEntry).where(
                RosterEntry.employee_id.in_(ids),
                RosterEntry.day >= monday,
                RosterEntry.day <= sunday,
            )
        )
    }
    with_usual = set(
        db.scalars(
            select(ShiftAssignment.employee_id).where(
                ShiftAssignment.employee_id.in_(ids),
                ShiftAssignment.start_date <= sunday,
                or_(ShiftAssignment.end_date.is_(None), ShiftAssignment.end_date >= monday),
            )
        )
    )
    result = []
    for r in rows:
        if r.id not in with_usual and not any(e == r.id for e, _ in changed):
            continue
        cells = []
        for offset in range(7):
            day = monday + timedelta(days=offset)
            shift = shifts.get((r.id, day))
            if (r.id, day) in changed:
                cells.append(
                    RosterCell(day, shift.name if shift else "Off", "changed" if shift else "off")
                )
            elif shift:
                cells.append(RosterCell(day, shift.name, "usual"))
            else:
                cells.append(RosterCell(day, "Off", "none"))
        result.append(RosterRow(r.id, r.full_name, cells))
    return result


def set_roster_day(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID | None,
    *,
    day: date | None,
    choice: str,
    note: str = "",
) -> None:
    """`choice` is a pattern ID, "off" for a day off, or "usual" to remove the change."""
    if employee_id is None:
        raise ValidationFailed(field_errors={"roster_employee_id": "Choose a person."})
    _require(db, actor, SCHEDULE, employee_id)
    if day is None:
        raise ValidationFailed(field_errors={"day": "Choose the day."})
    pattern_id: uuid.UUID | None = None
    if choice not in {"off", "usual"}:
        try:
            pattern_id = uuid.UUID(choice)
        except ValueError:
            pattern_id = None
        pattern = db.get(ShiftPattern, pattern_id) if pattern_id else None
        if pattern is None or not pattern.is_active:
            raise ValidationFailed(field_errors={"choice": "Choose a shift or a day off."})
    row = db.scalars(
        select(RosterEntry).where(RosterEntry.employee_id == employee_id, RosterEntry.day == day)
    ).one_or_none()
    before = {"shift": (str(row.pattern_id) if row.pattern_id else "off")} if row else {}
    if choice == "usual":
        if row is None:
            return
        db.delete(row)
    else:
        if row is None:
            row = RosterEntry(employee_id=employee_id, day=day, created_by=actor.user_id)
            db.add(row)
        row.pattern_id, row.note = pattern_id, note.strip()[:200] or None
    db.flush()
    record_by(
        db,
        actor,
        "attendance.roster.update",
        entity_type="roster_entry",
        entity_id=f"{employee_id}:{day.isoformat()}",
        changes=diff_changes(before, {} if choice == "usual" else {"shift": choice}),
    )
