"""Attendance (ADR 0019): clocking in and out, a day's status, corrections, overtime, and
the lists built on them (timesheet, who's in today, the monthly summary for payroll)."""

import math
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.attendance import schedule
from app.modules.attendance.files import ATTENDANCE_PHOTO
from app.modules.attendance.models import (
    AttendanceCorrection,
    AttendanceLocation,
    AttendanceRecord,
    OvertimeEntry,
)
from app.modules.attendance.schedule import Shift
from app.modules.attendance.schemas import (
    CorrectionView,
    DayView,
    OvertimeView,
    PersonDay,
    Position,
    SummaryRow,
)
from app.modules.hr import service as people
from app.modules.leave import service as leave
from app.org import service as org

# A clock-in with no clock-out is flagged this long after the shift ends (confirm).
MISSING_AFTER = timedelta(hours=4)
# Without a shift, a clock-in stays open this long (confirm).
UNSCHEDULED_OPEN = timedelta(hours=16)
MIN_NOTE = 5
EARTH_RADIUS_M = 6_371_000
LIST_SIZE = 500

APPROVE = "attendance.record.approve"


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def distance_m(lat1: Decimal, lng1: Decimal, lat2: Decimal, lng2: Decimal) -> int:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = p2 - p1, math.radians(float(lng2) - float(lng1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)))


def _snapshot(record: AttendanceRecord, shift: Shift | None) -> None:
    record.pattern_name = shift.name if shift else None
    record.scheduled_start = shift.start if shift else None
    record.scheduled_end = shift.end if shift else None
    record.break_minutes = shift.break_minutes if shift else 0
    record.late_grace_minutes = shift.late_grace_minutes if shift else 0
    record.early_grace_minutes = shift.early_grace_minutes if shift else 0


def _open_until(record: AttendanceRecord) -> datetime:
    """When a clock-in without a clock-out becomes "missing clock-out". A clock-in after
    the shift has ended is treated like one without a shift."""
    if record.scheduled_end is not None and record.clock_in_at < record.scheduled_end:
        return record.scheduled_end + MISSING_AFTER
    return record.clock_in_at + UNSCHEDULED_OPEN


# ------------------------------------------------------------------ clocking


def _me(db: Session, actor: Actor) -> uuid.UUID:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not _can(db, actor, "attendance.clock.use", employee_id):
        raise PermissionDenied()
    facts = people.status_and_account(db, employee_id)
    if facts is None or facts[0] == "terminated":
        raise Conflict("This person has left the organisation.")
    return employee_id


def open_record(db: Session, employee_id: uuid.UUID) -> AttendanceRecord | None:
    """The record they are clocked in on, if any (not yet flagged as missing a clock-out)."""
    now = clock.now()
    rows = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.clock_out_at.is_(None),
            AttendanceRecord.work_date >= clock.local_date(now) - timedelta(days=2),
        )
        .order_by(AttendanceRecord.work_date.desc())
    )
    return next((r for r in rows if now <= _open_until(r)), None)


def _work_day(db: Session, employee_id: uuid.UUID, now: datetime) -> tuple[date, Shift | None]:
    """The day a clock-in belongs to: yesterday while yesterday's night shift is still
    running, otherwise today."""
    today = clock.local_date(now)
    yesterday = today - timedelta(days=1)
    found = schedule.shifts(db, [employee_id], yesterday, today)
    previous = found.get((employee_id, yesterday))
    if previous is not None and now < previous.end:
        return yesterday, previous
    return today, found.get((employee_id, today))


def location_rules(db: Session, employee_id: uuid.UUID) -> tuple[AttendanceLocation | None, str]:
    """The clocking rules for the person's branch, and the branch's name."""
    branch_id = people.branches_of(db, [employee_id]).get(employee_id)
    if branch_id is None:
        return None, ""
    rules = db.scalars(
        select(AttendanceLocation).where(AttendanceLocation.branch_id == branch_id)
    ).one_or_none()
    return rules, people.branch_names(db).get(branch_id, "")


def _distance(rules: AttendanceLocation | None, position: Position | None) -> int | None:
    if rules is None or position is None or rules.latitude is None or rules.longitude is None:
        return None
    return distance_m(rules.latitude, rules.longitude, position.latitude, position.longitude)


def _check_geofence(db: Session, employee_id: uuid.UUID, position: Position | None) -> int | None:
    """Refuse a clock-in away from a geofenced branch, or without a location there
    (confirmed 2026-11-19). Returns the distance in metres when it can be worked out."""
    rules, branch = location_rules(db, employee_id)
    distance = _distance(rules, position)
    if rules is None or not rules.geofence_enabled:
        return distance
    if position is None or distance is None:
        raise ValidationFailed(
            field_errors={
                "location": f"Allow location so we can check you're at {branch}. "
                "Clocking in needs it here."
            }
        )
    if distance > rules.radius_m:
        away = f"{distance / 1000:.1f} km" if distance >= 1000 else f"{distance} m"
        raise ValidationFailed(
            field_errors={"location": f"You're about {away} from {branch}. Clock in there."}
        )
    return distance


def clock_in(
    db: Session,
    actor: Actor,
    *,
    position: Position | None,
    photo: tuple[str | None, str | None, bytes] | None = None,
) -> uuid.UUID:
    """Clock in (or back in, on a day already clocked out: the first clock-in stays).
    `photo` is (filename, declared type, bytes)."""
    employee_id = _me(db, actor)
    if open_record(db, employee_id) is not None:
        raise Conflict("You're already clocked in.")
    now = clock.now()
    day, shift = _work_day(db, employee_id, now)
    distance = _check_geofence(db, employee_id, position)
    rules, _ = location_rules(db, employee_id)
    record = db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.employee_id == employee_id, AttendanceRecord.work_date == day)
        .with_for_update()
    ).one_or_none()
    if record is not None:
        if record.clock_out_at is None:
            raise Conflict("This day is waiting for a clock-out correction.")
        record.clock_out_at = None
        record.out_latitude = record.out_longitude = None
        record.out_accuracy_m = record.out_distance_m = None
        _sync_overtime(db, record)
        record_by(
            db,
            actor,
            "attendance.clock.resume",
            entity_type="attendance_record",
            entity_id=str(record.id),
        )
        return record.id
    if rules is not None and rules.require_photo and not (photo and photo[2]):
        raise ValidationFailed(field_errors={"photo": "Take a photo to clock in."})
    record = AttendanceRecord(
        employee_id=employee_id,
        work_date=day,
        clock_in_at=now,
        in_latitude=position.latitude if position else None,
        in_longitude=position.longitude if position else None,
        in_accuracy_m=position.accuracy_m if position else None,
        in_distance_m=distance,
    )
    _snapshot(record, shift)
    db.add(record)
    db.flush()
    if photo and photo[2]:
        record.photo_file_id = files.upload(
            db,
            actor,
            purpose_key=ATTENDANCE_PHOTO.key,
            entity_id=str(record.id),
            filename=photo[0] or "clock-in.jpg",
            declared_mime=photo[1],
            data=photo[2],
        )
        record.photo_taken = True
    record_by(
        db,
        actor,
        "attendance.clock.in",
        entity_type="attendance_record",
        entity_id=str(record.id),
        changes={
            "work_date": {"from": None, "to": day.isoformat()},
            "distance_m": {"from": None, "to": distance},
        },
    )
    return record.id


def clock_out(db: Session, actor: Actor, *, position: Position | None) -> uuid.UUID:
    """Clock out. The location is recorded but not enforced: the geofence applies to
    clocking in."""
    employee_id = _me(db, actor)
    record = open_record(db, employee_id)
    if record is None:
        raise Conflict("You're not clocked in.")
    rules, _ = location_rules(db, employee_id)
    now = clock.now()
    record.clock_out_at = max(now, record.clock_in_at + timedelta(seconds=1))
    if position is not None:
        record.out_latitude, record.out_longitude = position.latitude, position.longitude
        record.out_accuracy_m = position.accuracy_m
        record.out_distance_m = _distance(rules, position)
    _sync_overtime(db, record)
    record_by(
        db,
        actor,
        "attendance.clock.out",
        entity_type="attendance_record",
        entity_id=str(record.id),
    )
    return record.id


def _sync_overtime(db: Session, record: AttendanceRecord) -> None:
    """Keep the record's overtime in step with its times: time past the scheduled end.
    A change in minutes sends it back for approval."""
    entry = db.scalars(
        select(OvertimeEntry).where(OvertimeEntry.record_id == record.id)
    ).one_or_none()
    minutes = 0
    if record.clock_out_at is not None and record.scheduled_end is not None:
        minutes = max(0, _minutes(record.scheduled_end, record.clock_out_at))
    if minutes <= 0:
        if entry is not None:
            db.delete(entry)
        return
    if entry is None:
        db.add(
            OvertimeEntry(
                record_id=record.id,
                employee_id=record.employee_id,
                work_date=record.work_date,
                minutes=minutes,
            )
        )
    elif entry.minutes != minutes:
        entry.minutes, entry.status = minutes, "pending"
        entry.decided_by = entry.decided_at = entry.decision_note = None


# ------------------------------------------------------------------ a day's status


def _day_not_clocked(base: DayView, shift: Shift | None, now: datetime) -> DayView:
    if shift is not None:
        base = replace(
            base, shift_name=shift.name, scheduled_start=shift.start, scheduled_end=shift.end
        )
    if base.leave_part == "full":
        return replace(base, status="on_leave")
    if base.holiday:
        return replace(base, status="holiday")
    if shift is None:
        return base
    if now < shift.start:
        return replace(base, status="upcoming")
    return replace(base, status="not_in" if now < shift.end else "absent")


def _day(
    employee_id: uuid.UUID,
    day: date,
    *,
    shift: Shift | None,
    record: AttendanceRecord | None,
    overtime: OvertimeEntry | None,
    away: tuple[str, str] | None,
    holiday: str | None,
    pending: bool,
    now: datetime,
) -> DayView:
    leave_name, part = away or (None, None)
    base = DayView(
        employee_id=employee_id,
        day=day,
        status="off",
        leave=leave_name,
        leave_part=part,
        holiday=holiday,
        pending_correction=pending,
    )
    if record is None:
        return _day_not_clocked(base, shift, now)
    start, end = record.scheduled_start, record.scheduled_end
    clock_in, clock_out = record.clock_in_at, record.clock_out_at
    if clock_out is None:
        status = "missing_clock_out" if now > _open_until(record) else "working"
    else:
        status = "done"
    late = early = worked = 0
    # Half-day leave: no lateness when the morning is off, no early leaving when the
    # afternoon is.
    if (
        start is not None
        and part != "morning"
        and clock_in > start + timedelta(minutes=record.late_grace_minutes)
    ):
        late = _minutes(start, clock_in)
    if (
        end is not None
        and clock_out is not None
        and part != "afternoon"
        and clock_out < end - timedelta(minutes=record.early_grace_minutes)
    ):
        early = _minutes(clock_out, end)
    if clock_out is not None:
        worked = max(0, _minutes(clock_in, clock_out) - record.break_minutes)
    return replace(
        base,
        status=status,
        shift_name=record.pattern_name,
        scheduled_start=start,
        scheduled_end=end,
        clock_in=clock_in,
        clock_out=clock_out,
        late_minutes=late,
        early_minutes=early,
        worked_minutes=worked,
        overtime_id=overtime.id if overtime else None,
        overtime_minutes=overtime.minutes if overtime else 0,
        overtime_status=overtime.status if overtime else None,
        record_id=record.id,
        photo_taken=record.photo_taken,
        photo_file_id=record.photo_file_id,
        in_distance_m=record.in_distance_m,
        corrected=record.corrected_at is not None,
    )


def days_for(
    db: Session, employee_ids: list[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, list[DayView]]:
    """Every day between two dates for each person, with its status."""
    if not employee_ids:
        return {}
    now = clock.now()
    shifts = schedule.shifts(db, employee_ids, start, end)
    records = {
        (r.employee_id, r.work_date): r
        for r in db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id.in_(employee_ids),
                AttendanceRecord.work_date >= start,
                AttendanceRecord.work_date <= end,
            )
        )
    }
    overtime = {
        o.record_id: o
        for o in db.scalars(
            select(OvertimeEntry).where(
                OvertimeEntry.record_id.in_([r.id for r in records.values()])
            )
        )
    }
    pending = {
        (employee_id, day)
        for employee_id, day in db.execute(
            select(AttendanceCorrection.employee_id, AttendanceCorrection.work_date).where(
                AttendanceCorrection.employee_id.in_(employee_ids),
                AttendanceCorrection.status == "pending",
                AttendanceCorrection.work_date >= start,
                AttendanceCorrection.work_date <= end,
            )
        ).all()
    }
    away = leave.approved_days(db, employee_ids, start, end)
    holidays = leave.holidays(db, start, end)
    result: dict[uuid.UUID, list[DayView]] = {}
    for employee_id in employee_ids:
        rows = []
        day = start
        while day <= end:
            key = (employee_id, day)
            record = records.get(key)
            rows.append(
                _day(
                    employee_id,
                    day,
                    shift=shifts.get(key),
                    record=record,
                    overtime=overtime.get(record.id) if record else None,
                    away=away.get(key),
                    holiday=holidays.get(day),
                    pending=key in pending,
                    now=now,
                )
            )
            day += timedelta(days=1)
        result[employee_id] = rows
    return result


def today_for(db: Session, employee_id: uuid.UUID) -> DayView:
    """The day the person is clocking on now (yesterday during a night shift)."""
    record = open_record(db, employee_id)
    day = record.work_date if record else _work_day(db, employee_id, clock.now())[0]
    return days_for(db, [employee_id], day, day)[employee_id][0]


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    return first, date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def timesheet(
    db: Session, actor: Actor, employee_id: uuid.UUID, year: int, month: int
) -> list[DayView]:
    if not _can(db, actor, "attendance.record.read", employee_id):
        raise NotFound()
    first, last = month_bounds(year, month)
    return days_for(db, [employee_id], first, last)[employee_id]


def _positions(db: Session, employee_ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    held = org.positions_held(db, employee_ids)
    return {e: (held[e][0][0] if held.get(e) else None) for e in employee_ids}


STATUS_ORDER = {
    "missing_clock_out": 0,
    "absent": 1,
    "not_in": 2,
    "working": 3,
    "done": 4,
    "upcoming": 5,
    "on_leave": 6,
    "holiday": 7,
    "off": 8,
}


def team_today(db: Session, actor: Actor, *, q: str = "") -> list[PersonDay]:
    """People in scope with a shift, a clock-in or leave today."""
    rows, _ = people.page_in_scope(db, actor, "attendance.record.read", q=q, size=LIST_SIZE)
    ids = [r.id for r in rows]
    today = clock.today()
    days = days_for(db, ids, today, today)
    held = _positions(db, ids)
    team = [
        PersonDay(r.full_name, held.get(r.id), days[r.id][0])
        for r in rows
        if days[r.id][0].scheduled or days[r.id][0].record_id or days[r.id][0].leave
    ]
    return sorted(team, key=lambda p: (STATUS_ORDER.get(p.day.status, 9), p.name))


def _summary_row(
    employee_id: uuid.UUID, name: str, number: str | None, days: list[DayView]
) -> SummaryRow:
    leave_days = Decimal(0)
    for d in days:
        if d.leave and d.scheduled:
            leave_days += 1 if d.leave_part == "full" else Decimal("0.5")
    return SummaryRow(
        employee_id=employee_id,
        name=name,
        employee_number=number,
        scheduled=sum(1 for d in days if d.scheduled and d.status != "holiday"),
        present=sum(1 for d in days if d.present),
        absent=sum(1 for d in days if d.status == "absent"),
        late=sum(1 for d in days if d.late),
        left_early=sum(1 for d in days if d.left_early),
        missing_clock_out=sum(1 for d in days if d.status == "missing_clock_out"),
        leave_days=leave_days,
        worked_minutes=sum(d.worked_minutes for d in days),
        overtime_approved=sum(d.overtime_minutes for d in days if d.overtime_status == "approved"),
        overtime_pending=sum(d.overtime_minutes for d in days if d.overtime_status == "pending"),
    )


def summary(
    db: Session,
    actor: Actor,
    year: int,
    month: int,
    *,
    q: str = "",
    page: int = 1,
    size: int = 50,
) -> tuple[list[SummaryRow], int]:
    """The month per person in scope, one page at a time, with the number of pages.
    Only people with a shift (or a clock-in) that month are listed."""
    rows, pages = people.page_in_scope(
        db, actor, "attendance.record.read", q=q, page=page, size=size
    )
    first, last = month_bounds(year, month)
    days = days_for(db, [r.id for r in rows], first, last)
    result = [
        _summary_row(r.id, r.full_name, r.employee_number, days[r.id])
        for r in rows
        if any(d.scheduled or d.record_id for d in days[r.id])
    ]
    return result, pages


def summary_all(db: Session, actor: Actor, year: int, month: int) -> list[SummaryRow]:
    """Everyone in scope, for the payroll export."""
    result, page, pages = [], 1, 1
    while page <= pages:
        rows, pages = summary(db, actor, year, month, page=page, size=LIST_SIZE)
        result.extend(rows)
        page += 1
    return result


# ------------------------------------------------------------------ deciding


def _owner_user(db: Session, employee_id: uuid.UUID) -> uuid.UUID | None:
    facts = people.status_and_account(db, employee_id)
    return facts[1] if facts else None


def may_decide(db: Session, actor: Actor, employee_id: uuid.UUID) -> bool:
    if not _can(db, actor, APPROVE, employee_id):
        return False
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        APPROVE,
        actor.user_id,
        {"owner_user_id": _owner_user(db, employee_id)},
        held,
    )


def _check_decider(db: Session, actor: Actor, employee_id: uuid.UUID) -> None:
    if may_decide(db, actor, employee_id):
        return
    if _can(db, actor, APPROVE, employee_id):
        raise PermissionDenied("You can't approve your own attendance.")
    raise NotFound()


def _note(approve: bool, note: str) -> str:
    note = note.strip()
    if not approve and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, so they know what to change."})
    return note


# ------------------------------------------------------------------ corrections


def request_correction(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    day: date | None,
    clock_in: time | None,
    clock_out: time | None,
    reason: str,
) -> uuid.UUID:
    """Ask to set a day's times. A clock-out earlier than the clock-in is the next day."""
    if not _can(db, actor, "attendance.correction.request", employee_id):
        raise PermissionDenied()
    errors = {}
    if day is None:
        errors["work_date"] = "Choose the day."
    elif day > clock.today():
        errors["work_date"] = "You can only correct days that have happened."
    if clock_in is None:
        errors["clock_in"] = "Enter the time you started."
    if clock_out is None:
        errors["clock_out"] = "Enter the time you finished."
    if len(reason.strip()) < MIN_NOTE:
        errors["reason"] = "Say what happened, so your manager can check it."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert day is not None and clock_in is not None and clock_out is not None  # noqa: S101
    in_at, out_at = schedule.at(day, clock_in), schedule.at(day, clock_out)
    if out_at <= in_at:
        out_at += timedelta(days=1)
    if out_at > clock.now():
        raise ValidationFailed(field_errors={"clock_out": "This time hasn't happened yet."})
    clash = db.scalar(
        select(AttendanceCorrection.id).where(
            AttendanceCorrection.employee_id == employee_id,
            AttendanceCorrection.work_date == day,
            AttendanceCorrection.status == "pending",
        )
    )
    if clash:
        raise Conflict("There's already a correction waiting for this day.")
    row = AttendanceCorrection(
        employee_id=employee_id,
        work_date=day,
        clock_in_at=in_at,
        clock_out_at=out_at,
        reason=reason.strip(),
        requested_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "attendance.correction.request",
        entity_type="attendance_correction",
        entity_id=str(row.id),
        changes={
            "work_date": {"from": None, "to": day.isoformat()},
            "clock_in": {"from": None, "to": in_at.isoformat()},
            "clock_out": {"from": None, "to": out_at.isoformat()},
        },
    )
    return row.id


def _locked_correction(db: Session, correction_id: uuid.UUID) -> AttendanceCorrection:
    row = db.scalars(
        select(AttendanceCorrection)
        .where(AttendanceCorrection.id == correction_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFound()
    return row


def can_cancel_correction(db: Session, actor: Actor, row: AttendanceCorrection) -> bool:
    return row.status == "pending" and (
        row.requested_by == actor.user_id
        or _can(db, actor, "attendance.correction.request", row.employee_id)
    )


def cancel_correction(db: Session, actor: Actor, correction_id: uuid.UUID) -> None:
    row = _locked_correction(db, correction_id)
    if not _can(db, actor, "attendance.record.read", row.employee_id):
        raise NotFound()
    if not can_cancel_correction(db, actor, row):
        raise Conflict("This correction has already been decided.")
    row.status = "cancelled"
    record_by(
        db,
        actor,
        "attendance.correction.cancel",
        entity_type="attendance_correction",
        entity_id=str(row.id),
    )


def decide_correction(
    db: Session, actor: Actor, correction_id: uuid.UUID, *, approve: bool, note: str = ""
) -> None:
    """Approving writes the times onto the day's record (creating it if they never
    clocked in), and recalculates overtime."""
    row = _locked_correction(db, correction_id)
    _check_decider(db, actor, row.employee_id)
    if row.status != "pending":
        raise Conflict("This correction has already been decided.")
    note = _note(approve, note)
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decided_at, row.decision_note = actor.user_id, clock.now(), note or None
    changes: dict[str, dict[str, object]] = {"status": {"from": "pending", "to": row.status}}
    if approve:
        record = db.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.employee_id == row.employee_id,
                AttendanceRecord.work_date == row.work_date,
            )
            .with_for_update()
        ).one_or_none()
        if record is None:
            record = AttendanceRecord(
                employee_id=row.employee_id,
                work_date=row.work_date,
                clock_in_at=row.clock_in_at,
                source="correction",
            )
            _snapshot(record, schedule.shift_on(db, row.employee_id, row.work_date))
            db.add(record)
        before_in, before_out = record.clock_in_at, record.clock_out_at
        record.clock_in_at, record.clock_out_at = row.clock_in_at, row.clock_out_at
        record.corrected_at = clock.now()
        db.flush()
        _sync_overtime(db, record)
        changes["clock_in"] = {"from": str(before_in), "to": row.clock_in_at.isoformat()}
        changes["clock_out"] = {
            "from": before_out.isoformat() if before_out else None,
            "to": row.clock_out_at.isoformat(),
        }
    record_by(
        db,
        actor,
        f"attendance.correction.{'approve' if approve else 'reject'}",
        entity_type="attendance_correction",
        entity_id=str(row.id),
        reason=note or None,
        changes=changes,
    )


def _deciders(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def _correction_views(
    db: Session, actor: Actor, rows: list[AttendanceCorrection]
) -> list[CorrectionView]:
    if not rows:
        return []
    names = people.names(db, {r.employee_id for r in rows})
    deciders = _deciders(db, {r.decided_by for r in rows})
    records = {
        (r.employee_id, r.work_date): r
        for r in db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id.in_({r.employee_id for r in rows}),
                AttendanceRecord.work_date.in_({r.work_date for r in rows}),
            )
        )
    }
    views = []
    for r in rows:
        record = records.get((r.employee_id, r.work_date))
        views.append(
            CorrectionView(
                id=r.id,
                employee_id=r.employee_id,
                employee_name=names.get(r.employee_id, ""),
                work_date=r.work_date,
                clock_in=r.clock_in_at,
                clock_out=r.clock_out_at,
                reason=r.reason,
                status=r.status,
                created_at=r.created_at,
                decided_by=deciders.get(r.decided_by) if r.decided_by else None,
                decision_note=r.decision_note,
                recorded_in=record.clock_in_at if record else None,
                recorded_out=record.clock_out_at if record else None,
                can_decide=r.status == "pending" and may_decide(db, actor, r.employee_id),
                can_cancel=can_cancel_correction(db, actor, r),
            )
        )
    return views


def corrections_for(
    db: Session, actor: Actor, employee_id: uuid.UUID, start: date, end: date
) -> list[CorrectionView]:
    if not _can(db, actor, "attendance.record.read", employee_id):
        return []
    rows = list(
        db.scalars(
            select(AttendanceCorrection)
            .where(
                AttendanceCorrection.employee_id == employee_id,
                AttendanceCorrection.work_date >= start,
                AttendanceCorrection.work_date <= end,
                AttendanceCorrection.status != "cancelled",
            )
            .order_by(AttendanceCorrection.created_at.desc())
        )
    )
    return _correction_views(db, actor, rows)


def corrections_waiting(db: Session, actor: Actor) -> list[CorrectionView]:
    if not engine.check(db, actor.user_id, APPROVE):
        return []
    rows = list(
        db.scalars(
            select(AttendanceCorrection)
            .where(
                AttendanceCorrection.status == "pending",
                engine.scope_filter(db, actor.user_id, APPROVE, AttendanceCorrection),
            )
            .order_by(AttendanceCorrection.work_date)
            .limit(200)
        )
    )
    return [v for v in _correction_views(db, actor, rows) if v.can_decide]


# ------------------------------------------------------------------ overtime


def decide_overtime(
    db: Session, actor: Actor, overtime_id: uuid.UUID, *, approve: bool, note: str = ""
) -> None:
    row = db.scalars(
        select(OvertimeEntry).where(OvertimeEntry.id == overtime_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFound()
    _check_decider(db, actor, row.employee_id)
    if row.status != "pending":
        raise Conflict("This overtime has already been decided.")
    note = _note(approve, note)
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decided_at, row.decision_note = actor.user_id, clock.now(), note or None
    record_by(
        db,
        actor,
        f"attendance.overtime.{'approve' if approve else 'reject'}",
        entity_type="overtime_entry",
        entity_id=str(row.id),
        reason=note or None,
        changes={
            "status": {"from": "pending", "to": row.status},
            "minutes": {"from": None, "to": row.minutes},
        },
    )


def overtime_waiting(db: Session, actor: Actor) -> list[OvertimeView]:
    if not engine.check(db, actor.user_id, APPROVE):
        return []
    rows = list(
        db.scalars(
            select(OvertimeEntry)
            .where(
                OvertimeEntry.status == "pending",
                engine.scope_filter(db, actor.user_id, APPROVE, OvertimeEntry),
            )
            .order_by(OvertimeEntry.work_date)
            .limit(200)
        )
    )
    if not rows:
        return []
    names = people.names(db, {r.employee_id for r in rows})
    records = {
        r.id: r
        for r in db.scalars(
            select(AttendanceRecord).where(AttendanceRecord.id.in_([o.record_id for o in rows]))
        )
    }
    views = []
    for o in rows:
        if not may_decide(db, actor, o.employee_id):
            continue
        record = records[o.record_id]
        views.append(
            OvertimeView(
                id=o.id,
                employee_id=o.employee_id,
                employee_name=names.get(o.employee_id, ""),
                work_date=o.work_date,
                minutes=o.minutes,
                shift_name=record.pattern_name,
                scheduled_end=record.scheduled_end,
                clock_out=record.clock_out_at,
                status=o.status,
                can_decide=True,
            )
        )
    return views


def waiting_count(db: Session, actor: Actor) -> int:
    return len(corrections_waiting(db, actor)) + len(overtime_waiting(db, actor))
