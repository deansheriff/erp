"""Attendance web routes (ADR 0019): clocking in, the timesheet and corrections, who's in
today with approvals, the monthly summary (and its CSV), shifts and roster, settings.
Routes translate forms and render; the attendance services do the work."""

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.authz.engine import Resource
from app.core import clock
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.formatting import format_minutes
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.attendance import schedule, service, settings
from app.modules.attendance.files import ATTENDANCE_PHOTO, PHOTO_DAYS
from app.modules.attendance.schemas import Position
from app.modules.hr import service as people
from app.web.access import require_actor
from app.web.forms import parse_clock_time, parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
ClockUpload = Annotated[ReceivedUpload, Depends(upload_body(ATTENDANCE_PHOTO.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# status: (badge tone, label)
STATUSES = {
    "off": ("neutral", "Off"),
    "holiday": ("info", "Public holiday"),
    "on_leave": ("neutral", "On leave"),
    "upcoming": ("neutral", "Not started"),
    "not_in": ("warning", "Not in yet"),
    "absent": ("danger", "Absent"),
    "working": ("success", "Working"),
    "done": ("success", "Done"),
    "missing_clock_out": ("danger", "Missing clock-out"),
}
DECISION_TONES = {
    "pending": "warning",
    "approved": "success",
    "rejected": "danger",
    "cancelled": "neutral",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db,
        actor,
        active_module="attendance",
        active_section=section,
        crumbs=["Attendance", section],
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _month(value: str) -> date:
    today = clock.today()
    try:
        year, number = (int(p) for p in value.split("-")) if value else (today.year, today.month)
        return date(year, number, 1)
    except ValueError:
        return date(today.year, today.month, 1)


def _month_nav(first: date) -> dict[str, str]:
    previous = date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return {
        "title": f"{MONTHS[first.month - 1]} {first.year}",
        "current": first.strftime("%Y-%m"),
        "prev": previous.strftime("%Y-%m"),
        "next": following.strftime("%Y-%m"),
    }


def _photo_viewers(db: Session, actor: Actor, employee_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """The people whose clock-in photos the actor may open (managers only see that one
    was taken)."""
    return {
        e
        for e in employee_ids
        if engine.check(db, actor.user_id, "attendance.photo.view", Resource(owner_employee_id=e))
    }


def _position(latitude: str, longitude: str, accuracy: str) -> Position | None:
    try:
        lat, lng = Decimal(latitude.strip()), Decimal(longitude.strip())
    except (InvalidOperation, AttributeError):
        return None
    if not (lat.is_finite() and lng.is_finite() and abs(lat) <= 90 and abs(lng) <= 180):
        return None
    try:
        metres = round(float(accuracy)) if accuracy.strip() else None
    except ValueError:
        metres = None
    return Position(round(lat, 6), round(lng, 6), metres)


# ------------------------------------------------------------------ clock in


def _unlinked(db: Session, actor: Actor, section: str) -> dict[str, Any]:
    """For an account with no HR record (e.g. the super administrator)."""
    return {
        "shell": _shell(db, actor, section),
        "linked": False,
        "errors": {},
        "notice": None,
        "page_title": section,
    }


def _clock_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None:
        return _unlinked(db, actor, "Clock in")
    today = service.today_for(db, employee_id)
    monday = today.day - timedelta(days=today.day.weekday())
    week = service.days_for(db, [employee_id], monday, monday + timedelta(days=6))[employee_id]
    rules, branch = service.location_rules(db, employee_id)
    open_now = service.open_record(db, employee_id) is not None
    return {
        "shell": _shell(db, actor, "Clock in"),
        "today": today,
        "week": week,
        "now": clock.now(),
        "clocked_in": open_now,
        "worked_so_far": (
            format_minutes(int((clock.now() - today.clock_in).total_seconds() // 60))
            if open_now and today.clock_in
            else None
        ),
        "branch": branch,
        "geofence": bool(rules and rules.geofence_enabled),
        "needs_photo": bool(rules and rules.require_photo) and today.record_id is None,
        "photo_days": PHOTO_DAYS,
        "timezone": get_settings().timezone,
        "statuses": STATUSES,
        "errors": {},
        "notice": None,
        "page_title": "Clock in",
        **extra,
    }


@router.get("/attendance", response_class=HTMLResponse)
@requires("attendance.clock.use")
@screen("attendance.clock")
def clock_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    notices = {"in": "Clocked in.", "out": "Clocked out. Have a good evening."}
    context = _clock_context(db, actor, notice=notices.get(notice))
    return _render(request, "attendance/clock.html", context)


@router.post("/attendance/clock", response_class=HTMLResponse)
@requires("attendance.clock.use")
@screen("attendance.clock")
def clock_in_or_out(request: Request, db: Db, actor: CurrentActor, upload: ClockUpload) -> Response:
    form = upload.fields
    action = form.get("action", "")
    position = _position(
        form.get("latitude", ""), form.get("longitude", ""), form.get("accuracy", "")
    )
    photo = (upload.filename, upload.content_type, upload.data) if upload.data else None
    try:
        if upload.errors:  # e.g. a photo over the size limit
            raise ValidationFailed(field_errors={"photo": next(iter(upload.errors.values()))})
        with db.begin_nested():
            if action == "out":
                service.clock_out(db, actor, position=position)
            elif action == "in":
                service.clock_in(db, actor, position=position, photo=photo)
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        if "file" in errors:
            errors["photo"] = errors.pop("file")
        context = _clock_context(db, actor, errors=errors)
        return _render(request, "attendance/clock.html", context, 422)
    if photo:
        files.request_scan()
    return _see_other(f"/attendance?notice={action}")


# ------------------------------------------------------------------ timesheet


def _timesheet_context(
    db: Session,
    actor: Actor,
    *,
    month: str,
    person: str,
    correct: str = "",
    **extra: Any,
) -> dict[str, Any]:
    first = _month(month)
    mine = people.employee_of_user(db, actor.user_id)
    others: list[tuple[uuid.UUID, str]] = []
    if engine.check(db, actor.user_id, "attendance.record.approve") or engine.check(
        db, actor.user_id, "attendance.schedule.manage"
    ):
        rows, _ = people.page_in_scope(db, actor, "attendance.record.read", size=500)
        others = [(r.id, r.full_name) for r in rows]
    employee_id = _uuid(person) or mine or (others[0][0] if others else None)
    if employee_id is None:
        return _unlinked(db, actor, "Timesheet")
    days = service.timesheet(db, actor, employee_id, first.year, first.month)
    last = days[-1].day
    corrections = service.corrections_for(db, actor, employee_id, first, last)
    correct_day = parse_day(correct)
    chosen = next((d for d in days if d.day == correct_day), None)
    may_correct = bool(
        engine.check(
            db,
            actor.user_id,
            "attendance.correction.request",
            Resource(owner_employee_id=employee_id),
        )
    )
    return {
        "shell": _shell(db, actor, "Timesheet"),
        "days": days,
        "employee_id": employee_id,
        "person_name": people.names(db, {employee_id}).get(employee_id, ""),
        "is_me": employee_id == mine,
        "people": others,
        "corrections": corrections,
        "may_correct": may_correct,
        "values": {
            "work_date": correct or "",
            "clock_in": f"{chosen.clock_in:%H:%M}" if chosen and chosen.clock_in else "",
        },
        "photo_viewers": _photo_viewers(db, actor, {employee_id}),
        "totals": {
            "scheduled": sum(1 for d in days if d.scheduled and d.status != "holiday"),
            "present": sum(1 for d in days if d.present),
            "worked": sum(d.worked_minutes for d in days),
            "late": sum(1 for d in days if d.late),
            "overtime": sum(d.overtime_minutes for d in days if d.overtime_status == "approved"),
        },
        "today": clock.today(),
        "statuses": STATUSES,
        "tones": DECISION_TONES,
        "person": str(employee_id) if employee_id != mine else "",
        "errors": {},
        "notice": None,
        "page_title": "Timesheet",
        **_month_nav(first),
        **extra,
    }


@router.get("/attendance/timesheet", response_class=HTMLResponse)
@requires("attendance.record.read")
@screen("attendance.timesheet")
def timesheet(
    request: Request,
    db: Db,
    actor: CurrentActor,
    month: str = "",
    person: str = "",
    correct: str = "",
    notice: str = "",
) -> HTMLResponse:
    notices = {"sent": "Correction sent for approval.", "cancelled": "Correction cancelled."}
    context = _timesheet_context(
        db, actor, month=month, person=person, correct=correct, notice=notices.get(notice)
    )
    return _render(request, "attendance/timesheet.html", context)


@router.post("/attendance/corrections", response_class=HTMLResponse)
@requires("attendance.correction.request")
@screen("attendance.timesheet")
def request_correction(
    request: Request,
    db: Db,
    actor: CurrentActor,
    person: FormStr = "",
    work_date: FormStr = "",
    clock_in: FormStr = "",
    clock_out: FormStr = "",
    reason: FormStr = "",
) -> Response:
    employee_id = _uuid(person) or people.employee_of_user(db, actor.user_id)
    if employee_id is None:
        raise NotFound()
    day = parse_day(work_date)
    try:
        with db.begin_nested():
            service.request_correction(
                db,
                actor,
                employee_id,
                day=day,
                clock_in=parse_clock_time(clock_in),
                clock_out=parse_clock_time(clock_out),
                reason=reason,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _timesheet_context(
            db,
            actor,
            month=day.strftime("%Y-%m") if day else "",
            person=person,
            errors=_errors(exc),
        )
        context["values"] = {
            "work_date": work_date,
            "clock_in": clock_in,
            "clock_out": clock_out,
            "reason": reason,
        }
        return _render(request, "attendance/timesheet.html", context, 422)
    month = day.strftime("%Y-%m") if day else ""
    who = f"&person={person}" if person else ""
    return _see_other(f"/attendance/timesheet?month={month}{who}&notice=sent")


@router.post("/attendance/corrections/{correction_id}/cancel", response_class=HTMLResponse)
@requires_login
@screen("attendance.timesheet")
def cancel_correction(db: Db, actor: CurrentActor, correction_id: uuid.UUID) -> Response:
    service.cancel_correction(db, actor, correction_id)
    return _see_other("/attendance/timesheet?notice=cancelled")


# ------------------------------------------------------------------ team today


def _team_context(db: Session, actor: Actor, *, q: str = "", **extra: Any) -> dict[str, Any]:
    team = service.team_today(db, actor, q=q)
    counts: dict[str, int] = {}
    for person in team:
        counts[person.day.status] = counts.get(person.day.status, 0) + 1
    return {
        "shell": _shell(db, actor, "Team today"),
        "team": team,
        "counts": counts,
        "late": sum(1 for p in team if p.day.late),
        "corrections": service.corrections_waiting(db, actor),
        "overtime": service.overtime_waiting(db, actor),
        "photo_viewers": _photo_viewers(db, actor, {p.day.employee_id for p in team}),
        "today": clock.today(),
        "q": q,
        "statuses": STATUSES,
        "errors": {},
        "failed": "",
        "note": "",
        "notice": None,
        "page_title": "Team today",
        **extra,
    }


@router.get("/attendance/team", response_class=HTMLResponse)
@requires("attendance.record.read")
@screen("attendance.team")
def team_today(
    request: Request, db: Db, actor: CurrentActor, q: str = "", notice: str = ""
) -> HTMLResponse:
    notices = {"approved": "Approved.", "rejected": "Rejected. They can see your note."}
    context = _team_context(db, actor, q=q, notice=notices.get(notice))
    return _render(request, "attendance/team.html", context)


def _decide(
    request: Request,
    db: Session,
    actor: Actor,
    kind: str,
    item_id: uuid.UUID,
    decision: str,
    note: str,
) -> Response:
    if decision not in {"approve", "reject"}:
        raise NotFound()
    decide_item = service.decide_correction if kind == "corrections" else service.decide_overtime
    try:
        with db.begin_nested():
            decide_item(db, actor, item_id, approve=decision == "approve", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _team_context(db, actor, errors=_errors(exc), failed=str(item_id), note=note)
        return _render(request, "attendance/team.html", context, 422)
    outcome = "approved" if decision == "approve" else "rejected"
    return _see_other(f"/attendance/team?notice={outcome}#approvals")


@router.post("/attendance/corrections/{correction_id}/{decision}", response_class=HTMLResponse)
@requires_login
@screen("attendance.team")
def decide_correction(
    request: Request,
    db: Db,
    actor: CurrentActor,
    correction_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
) -> Response:
    return _decide(request, db, actor, "corrections", correction_id, decision, note)


@router.post("/attendance/overtime/{overtime_id}/{decision}", response_class=HTMLResponse)
@requires_login
@screen("attendance.team")
def decide_overtime(
    request: Request,
    db: Db,
    actor: CurrentActor,
    overtime_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
) -> Response:
    return _decide(request, db, actor, "overtime", overtime_id, decision, note)


# ------------------------------------------------------------------ monthly summary


@router.get("/attendance/summary", response_class=HTMLResponse)
@requires("attendance.record.read")
@screen("attendance.summary")
def monthly_summary(
    request: Request, db: Db, actor: CurrentActor, month: str = "", q: str = "", page: int = 1
) -> HTMLResponse:
    first = _month(month)
    rows, pages = service.summary(db, actor, first.year, first.month, q=q, page=max(page, 1))
    context = {
        "shell": _shell(db, actor, "Monthly summary"),
        "rows": rows,
        "pages": pages,
        "page": min(max(page, 1), pages),
        "q": q,
        "page_title": "Monthly summary",
        **_month_nav(first),
    }
    return _render(request, "attendance/summary.html", context)


CSV_COLUMNS = (
    "Employee number",
    "Name",
    "Scheduled days",
    "Present",
    "Absent",
    "Late",
    "Left early",
    "Missing clock-out",
    "Leave days",
    "Hours worked",
    "Overtime hours approved",
    "Overtime hours waiting",
)


def _hours(minutes: int) -> str:
    return f"{Decimal(minutes) / 60:.2f}"


def _cell(value: str) -> str:
    """Stop spreadsheet apps reading a value as a formula."""
    return f"'{value}" if value[:1] in {"=", "+", "-", "@"} else value


@router.get("/attendance/summary.csv")
@requires("attendance.record.read")
def summary_csv(db: Db, actor: CurrentActor, month: str = "") -> Response:
    first = _month(month)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in service.summary_all(db, actor, first.year, first.month):
        writer.writerow(
            (
                _cell(r.employee_number or ""),
                _cell(r.name),
                r.scheduled,
                r.present,
                r.absent,
                r.late,
                r.left_early,
                r.missing_clock_out,
                r.leave_days,
                _hours(r.worked_minutes),
                _hours(r.overtime_approved),
                _hours(r.overtime_pending),
            )
        )
    filename = f"attendance-{first:%Y-%m}.csv"
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ shifts and roster


def _roster_context(
    db: Session, actor: Actor, *, week: str = "", q: str = "", **extra: Any
) -> dict[str, Any]:
    start = parse_day(week) or clock.today()
    monday = start - timedelta(days=start.weekday())
    rows, _ = people.page_in_scope(db, actor, "attendance.schedule.manage", size=500)
    return {
        "shell": _shell(db, actor, "Shifts & roster"),
        "assignments": settings.assignments(db, actor, q=q),
        "roster": settings.roster_week(db, actor, monday, q=q),
        "week_days": [monday + timedelta(days=i) for i in range(7)],
        "monday": monday,
        "prev_week": (monday - timedelta(days=7)).isoformat(),
        "next_week": (monday + timedelta(days=7)).isoformat(),
        "people": [(r.id, r.full_name) for r in rows],
        "patterns": [
            (p.id, schedule.label(p)) for p in settings.list_patterns(db, active_only=True)
        ],
        "today": clock.today(),
        "q": q,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Shifts & roster",
        **extra,
    }


@router.get("/attendance/roster", response_class=HTMLResponse)
@requires("attendance.schedule.manage")
@screen("attendance.roster")
def roster(
    request: Request, db: Db, actor: CurrentActor, week: str = "", q: str = "", notice: str = ""
) -> HTMLResponse:
    context = _roster_context(db, actor, week=week, q=q, notice="Saved." if notice else None)
    return _render(request, "attendance/roster.html", context)


@router.post("/attendance/roster/assignments", response_class=HTMLResponse)
@requires("attendance.schedule.manage")
@screen("attendance.roster")
def assign_shift(
    request: Request,
    db: Db,
    actor: CurrentActor,
    employee_id: FormStr = "",
    pattern_id: FormStr = "",
    start_date: FormStr = "",
    end_date: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            settings.assign(
                db,
                actor,
                _uuid(employee_id),
                pattern_id=_uuid(pattern_id),
                start=parse_day(start_date),
                end=parse_day(end_date),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "employee_id": employee_id,
            "pattern_id": pattern_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        context = _roster_context(db, actor, values=values, errors=_errors(exc))
        return _render(request, "attendance/roster.html", context, 422)
    return _see_other("/attendance/roster?notice=1#usual")


@router.post("/attendance/roster/assignments/{assignment_id}/end", response_class=HTMLResponse)
@requires("attendance.schedule.manage")
@screen("attendance.roster")
def end_shift(
    db: Db, actor: CurrentActor, assignment_id: uuid.UUID, last_day: FormStr = ""
) -> Response:
    settings.end_assignment(db, actor, assignment_id, last_day=parse_day(last_day))
    return _see_other("/attendance/roster?notice=1#usual")


@router.post("/attendance/roster/days", response_class=HTMLResponse)
@requires("attendance.schedule.manage")
@screen("attendance.roster")
def change_day(
    request: Request,
    db: Db,
    actor: CurrentActor,
    roster_employee_id: FormStr = "",
    day: FormStr = "",
    choice: FormStr = "",
    note: FormStr = "",
) -> Response:
    when = parse_day(day)
    try:
        with db.begin_nested():
            settings.set_roster_day(
                db, actor, _uuid(roster_employee_id), day=when, choice=choice, note=note
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "roster_employee_id": roster_employee_id,
            "day": day,
            "choice": choice,
            "note": note,
        }
        context = _roster_context(db, actor, week=day, values=values, errors=_errors(exc))
        return _render(request, "attendance/roster.html", context, 422)
    return _see_other(f"/attendance/roster?week={day}&notice=1#roster")


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Settings"),
        "patterns": settings.list_patterns(db),
        "locations": settings.locations(db),
        "weekdays": list(enumerate(settings.WEEKDAYS)),
        "default_radius": settings.DEFAULT_RADIUS_M,
        "editing": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Attendance settings",
        **extra,
    }


@router.get("/attendance/settings", response_class=HTMLResponse)
@requires("attendance.settings.manage")
@screen("attendance.settings")
def attendance_settings(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(db, actor, editing=edit, notice="Saved." if notice else None)
    return _render(request, "attendance/settings.html", context)


@router.post("/attendance/settings/patterns", response_class=HTMLResponse)
@requires("attendance.settings.manage")
@screen("attendance.settings")
def save_pattern(
    request: Request,
    db: Db,
    actor: CurrentActor,
    pattern_id: FormStr = "",
    name: FormStr = "",
    start_time: FormStr = "",
    end_time: FormStr = "",
    break_minutes: FormStr = "0",
    weekdays: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    late_grace: FormStr = "15",
    early_grace: FormStr = "15",
    is_active: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            settings.save_pattern(
                db,
                actor,
                _uuid(pattern_id),
                name=name,
                start_time=start_time,
                end_time=end_time,
                break_minutes=break_minutes,
                weekdays=weekdays,
                late_grace=late_grace,
                early_grace=early_grace,
                is_active=bool(is_active) or not pattern_id,
            )
    except (ValidationFailed, Conflict) as exc:
        values = {
            "name": name,
            "start_time": start_time,
            "end_time": end_time,
            "break_minutes": break_minutes,
            "weekdays": "".join(weekdays),
            "late_grace": late_grace,
            "early_grace": early_grace,
        }
        context = _settings_context(
            db,
            actor,
            errors=_errors(exc),
            values=values,
            editing=f"pattern:{pattern_id or 'new'}",
        )
        return _render(request, "attendance/settings.html", context, 422)
    return _see_other("/attendance/settings?notice=1#patterns")


@router.post("/attendance/settings/locations/{branch_id}", response_class=HTMLResponse)
@requires("attendance.settings.manage")
@screen("attendance.settings")
def save_location(
    request: Request,
    db: Db,
    actor: CurrentActor,
    branch_id: uuid.UUID,
    geofence_enabled: FormStr = "",
    latitude: FormStr = "",
    longitude: FormStr = "",
    radius_m: FormStr = "",
    require_photo: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            settings.save_location(
                db,
                actor,
                branch_id,
                geofence_enabled=bool(geofence_enabled),
                latitude=latitude,
                longitude=longitude,
                radius_m=radius_m,
                require_photo=bool(require_photo),
            )
    except (ValidationFailed, Conflict) as exc:
        values = {
            "geofence_enabled": geofence_enabled,
            "latitude": latitude,
            "longitude": longitude,
            "radius_m": radius_m,
            "require_photo": require_photo,
        }
        context = _settings_context(
            db, actor, errors=_errors(exc), values=values, editing=f"location:{branch_id}"
        )
        return _render(request, "attendance/settings.html", context, 422)
    return _see_other(f"/attendance/settings?notice=1#branch-{branch_id}")
