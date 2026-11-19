"""Leave web routes (ADR 0018): my leave and requesting it, approvals, the team calendar,
balances and settings. Routes translate forms and render; the leave services do the work."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.leave import balances, days, service, settings
from app.modules.leave.files import LEAVE_CERTIFICATE
from app.modules.leave.models import LeaveType
from app.org import service as org
from app.org.models import Grade
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
CertificateUpload = Annotated[ReceivedUpload, Depends(upload_body(LEAVE_CERTIFICATE.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

STATUS_TONES = {
    "pending": "warning",
    "approved": "success",
    "rejected": "danger",
    "cancelled": "neutral",
}
MONTHS = [
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
]


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
        db, actor, active_module="leave", active_section=section, crumbs=["Leave", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _me(db: Session, actor: Actor) -> uuid.UUID:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None:
        raise NotFound()
    return employee_id


# ------------------------------------------------------------------ my leave


@router.get("/leave", response_class=HTMLResponse)
@requires("leave.request.read")
@screen("leave.my")
def my_leave(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    employee_id = people.employee_of_user(db, actor.user_id)
    year = clock.today().year
    notices = {
        "sent": "Request sent.",
        "cancelled": "Request cancelled.",
        "certificate": "Certificate uploaded. It's checked for viruses before anyone opens it.",
    }
    mine = service.my_requests(db, actor, employee_id) if employee_id else []
    today = clock.today()
    context = {
        "shell": _shell(db, actor, "My leave"),
        "linked": employee_id is not None,
        "balances": balances.balances(db, [employee_id], year)[employee_id] if employee_id else [],
        "upcoming": [
            r for r in mine if r.end_date >= today and r.status in {"pending", "approved"}
        ],
        "history": [
            r for r in mine if not (r.end_date >= today and r.status in {"pending", "approved"})
        ],
        "year": year,
        "tones": STATUS_TONES,
        "notice": notices.get(notice),
        "errors": {},
        "page_title": "My leave",
    }
    return _render(request, "leave/my.html", context)


def _form_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    employee_id = people.employee_of_user(db, actor.user_id)
    year = clock.today().year
    manager = org.manager_of(db, employee_id, clock.today()) if employee_id else None
    return {
        "shell": _shell(db, actor, "Request leave"),
        "types": service.active_types(db),
        "balances": {
            b.leave_type_id: b
            for b in (
                balances.balances(db, [employee_id], year)[employee_id] if employee_id else []
            )
        },
        "manager": people.names(db, {manager}).get(manager) if manager else None,
        "values": {},
        "errors": {},
        "summary": None,
        "page_title": "Request leave",
        "back_href": "/leave",
        **extra,
    }


@router.get("/leave/new", response_class=HTMLResponse)
@requires("leave.request.create")
@screen("leave.request")
def new_request(request: Request, db: Db, actor: CurrentActor, type: str = "") -> HTMLResponse:
    _me(db, actor)
    context = _form_context(db, actor, values={"leave_type_id": type})
    return _render(request, "leave/request.html", context)


@router.post("/leave/new", response_class=HTMLResponse)
@requires("leave.request.create")
@screen("leave.request")
def send_request(
    request: Request,
    db: Db,
    actor: CurrentActor,
    leave_type_id: FormStr = "",
    start_date: FormStr = "",
    end_date: FormStr = "",
    start_half: FormStr = "",
    end_half: FormStr = "",
    reason: FormStr = "",
    check: FormStr = "",
) -> Response:
    employee_id = _me(db, actor)
    values = {
        "leave_type_id": leave_type_id,
        "start_date": start_date,
        "end_date": end_date,
        "start_half": start_half,
        "end_half": end_half,
        "reason": reason,
    }
    start, end = parse_day(start_date), parse_day(end_date or start_date)
    try:
        if check:  # "Check days" without sending
            leave_type = db.get(LeaveType, _uuid(leave_type_id)) if _uuid(leave_type_id) else None
            if leave_type is None or start is None or end is None:
                raise ValidationFailed(field_errors={"form": "Choose a type and dates first."})
            used, balance = service.preview(
                db,
                employee_id,
                leave_type,
                start,
                end,
                start_half=bool(start_half),
                end_half=bool(end_half),
            )
            summary = {
                "days": used,
                "balance": balance,
                "holidays": days.holidays_between(db, start, end),
                "certificate": service.needs_certificate(leave_type, used),
            }
            context = _form_context(db, actor, values=values, summary=summary)
            return _render(request, "leave/request.html", context)
        with db.begin_nested():
            request_id = service.request_leave(
                db,
                actor,
                employee_id,
                leave_type_id=_uuid(leave_type_id),
                start=start,
                end=end,
                start_half=bool(start_half),
                end_half=bool(end_half),
                reason=reason,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _form_context(db, actor, values=values, errors=_errors(exc))
        return _render(request, "leave/request.html", context, 422)
    return _see_other(f"/leave?notice=sent#r-{request_id}")


@router.post("/leave/requests/{request_id}/certificate", response_class=HTMLResponse)
@requires("leave.request.create")
@screen("leave.my")
def upload_certificate(
    db: Db, actor: CurrentActor, request_id: uuid.UUID, upload: CertificateUpload
) -> Response:
    if upload.errors:
        raise ValidationFailed(field_errors=upload.errors)
    service.attach_certificate(
        db,
        actor,
        request_id,
        filename=upload.filename,
        declared_mime=upload.content_type,
        data=upload.data,
    )
    files.request_scan()
    return _see_other(f"/leave?notice=certificate#r-{request_id}")


@router.post("/leave/requests/{request_id}/cancel", response_class=HTMLResponse)
@requires_login
@screen("leave.my")
def cancel_request(
    db: Db, actor: CurrentActor, request_id: uuid.UUID, return_to: FormStr = ""
) -> Response:
    service.cancel(db, actor, request_id)
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else ""
    return _see_other(target or "/leave?notice=cancelled")


# ------------------------------------------------------------------ approvals


def _approvals_context(
    db: Session, actor: Actor, tab: str, selected: str, **extra: Any
) -> dict[str, Any]:
    deciders = ("leave.request.approve", "leave.request.approve_hr")
    if not any(engine.check(db, actor.user_id, p) for p in deciders):
        raise PermissionDenied()
    tab = "decided" if tab == "decided" else "waiting"
    rows = service.inbox(db, actor) if tab == "waiting" else service.decided_by_me(db, actor)
    chosen = next((r for r in rows if str(r.id) == selected), rows[0] if rows else None)
    balance = None
    if chosen is not None:
        leave_type = db.get(LeaveType, chosen.leave_type_id)
        if leave_type is not None:
            balance = balances.balance(db, chosen.employee_id, leave_type, chosen.start_date.year)
    return {
        "shell": _shell(db, actor, "Approvals"),
        "tab": tab,
        "rows": rows,
        "waiting_count": len(service.inbox(db, actor)) if tab == "decided" else len(rows),
        "selected": chosen,
        "balance": balance,
        "away": service.away_alongside(db, actor, chosen) if chosen else [],
        "tones": STATUS_TONES,
        "errors": {},
        "note": "",
        "page_title": "Leave approvals",
        **extra,
    }


@router.get("/leave/approvals", response_class=HTMLResponse)
@requires_login
@screen("leave.approvals")
def approvals(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "",
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    notices = {"approved": "Approved.", "rejected": "Rejected. They can see your note."}
    context = _approvals_context(db, actor, tab, selected, notice=notices.get(notice))
    return _render(request, "leave/approvals.html", context)


@router.post("/leave/requests/{request_id}/{decision}", response_class=HTMLResponse)
@requires_login
@screen("leave.approvals")
def decide(
    request: Request,
    db: Db,
    actor: CurrentActor,
    request_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
) -> Response:
    if decision not in {"approve", "reject"}:
        raise NotFound()
    try:
        with db.begin_nested():
            service.decide(db, actor, request_id, approve=decision == "approve", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _approvals_context(
            db, actor, "waiting", str(request_id), errors=_errors(exc), note=note
        )
        return _render(request, "leave/approvals.html", context, 422)
    outcome = "approved" if decision == "approve" else "rejected"
    return _see_other(f"/leave/approvals?notice={outcome}")


# ------------------------------------------------------------------ calendar


@router.get("/leave/calendar", response_class=HTMLResponse)
@requires("leave.request.read")
@screen("leave.calendar")
def team_calendar(
    request: Request, db: Db, actor: CurrentActor, month: str = "", department: str = ""
) -> HTMLResponse:
    today = clock.today()
    try:
        year, number = (int(p) for p in month.split("-")) if month else (today.year, today.month)
        first = date(year, number, 1)
    except ValueError:
        first = date(today.year, today.month, 1)
    length = (date(first.year + first.month // 12, first.month % 12 + 1, 1) - first).days
    month_days = [date(first.year, first.month, d) for d in range(1, length + 1)]
    holidays = days.holidays_between(db, month_days[0], month_days[-1])
    prev_month = date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
    next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    rows = service.calendar(db, actor, first.year, first.month, department=_uuid(department))
    context = {
        "shell": _shell(db, actor, "Team calendar"),
        "rows": rows,
        "month_days": month_days,
        "weekend": days.WEEKEND,
        "holidays": holidays,
        "today": today,
        "title": f"{MONTHS[first.month - 1]} {first.year}",
        "prev": prev_month.strftime("%Y-%m"),
        "next": next_month.strftime("%Y-%m"),
        "current": first.strftime("%Y-%m"),
        "department": department,
        "departments": [(d.id, d.name) for d in org.list_departments(db)],
        "away_today": sum(
            1
            for r in rows
            if first.year == today.year
            and first.month == today.month
            and any(s <= today.day <= e for s, e, *_ in r.bars)
        ),
        "page_title": "Team calendar",
    }
    return _render(request, "leave/calendar.html", context)


# ------------------------------------------------------------------ balances


def _balances_context(
    db: Session, actor: Actor, *, year: int, q: str, page: int, selected: str, **extra: Any
) -> dict[str, Any]:
    rows, by_employee, pages = settings.balance_page(db, actor, year=year, q=q, page=page)
    chosen = None
    if selected and ":" in selected:
        eid, tid = (_uuid(p) for p in selected.split(":", 1))
        if eid in by_employee:
            chosen = next((b for b in by_employee[eid] if b.leave_type_id == tid), None)
    names = {r.id: r.full_name for r in rows}
    return {
        "shell": _shell(db, actor, "Balances"),
        "rows": rows,
        "balances": by_employee,
        "pages": pages,
        "page": min(page, pages),
        "year": year,
        "years": [year - 1, year, year + 1],
        "q": q,
        "selected": chosen,
        "selected_name": names.get(chosen.employee_id) if chosen else None,
        "history": settings.adjustments(db, chosen.employee_id, chosen.leave_type_id, year)
        if chosen
        else [],
        "can_adjust": bool(engine.check(db, actor.user_id, "leave.balance.adjust")),
        "values": {},
        "errors": {},
        "page_title": "Leave balances",
        **extra,
    }


@router.get("/leave/balances", response_class=HTMLResponse)
@requires("leave.balance.read")
@screen("leave.balances")
def balance_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    year: int = 0,
    q: str = "",
    page: int = 1,
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _balances_context(
        db,
        actor,
        year=year or clock.today().year,
        q=q,
        page=max(page, 1),
        selected=selected,
        notice="Balance adjusted." if notice else None,
    )
    return _render(request, "leave/balances.html", context)


@router.post("/leave/balances/adjust", response_class=HTMLResponse)
@requires("leave.balance.adjust")
@screen("leave.balances")
def adjust_balance(
    request: Request,
    db: Db,
    actor: CurrentActor,
    employee_id: FormStr = "",
    leave_type_id: FormStr = "",
    year: FormStr = "",
    days_change: FormStr = "",
    reason: FormStr = "",
) -> Response:
    the_year = int(year) if year.isdigit() else clock.today().year
    employee = _uuid(employee_id)
    if employee is None:
        raise NotFound()
    try:
        with db.begin_nested():
            settings.adjust(
                db,
                actor,
                employee,
                leave_type_id=_uuid(leave_type_id),
                year=the_year,
                days=days_change,
                reason=reason,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _balances_context(
            db,
            actor,
            year=the_year,
            q="",
            page=1,
            selected=f"{employee_id}:{leave_type_id}",
            values={"days_change": days_change, "reason": reason},
            errors=_errors(exc),
        )
        return _render(request, "leave/balances.html", context, 422)
    return _see_other(
        f"/leave/balances?year={the_year}&selected={employee_id}:{leave_type_id}&notice=1"
    )


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, year: int, **extra: Any) -> dict[str, Any]:

    return {
        "shell": _shell(db, actor, "Settings"),
        "types": settings.list_types(db),
        "holidays": settings.holidays(db, year),
        "year": year,
        "grades": [(g.id, g.name) for g in db.scalars(select(Grade).order_by(Grade.rank))],
        "months": MONTHS,
        "errors": {},
        "editing": "",
        "page_title": "Leave settings",
        **extra,
    }


@router.get("/leave/settings", response_class=HTMLResponse)
@requires("leave.settings.manage")
@screen("leave.settings")
def leave_settings(
    request: Request, db: Db, actor: CurrentActor, year: int = 0, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(
        db, actor, year or clock.today().year, editing=edit, notice="Saved." if notice else None
    )
    return _render(request, "leave/settings.html", context)


@router.post("/leave/settings/types", response_class=HTMLResponse)
@requires("leave.settings.manage")
@screen("leave.settings")
def save_type(
    request: Request,
    db: Db,
    actor: CurrentActor,
    type_id: FormStr = "",
    name: FormStr = "",
    paid: FormStr = "",
    counting: FormStr = "working",
    has_balance: FormStr = "",
    accrual: FormStr = "yearly",
    carry_over_max_days: FormStr = "0",
    carry_over_expiry_month: FormStr = "3",
    requires_hr_approval: FormStr = "",
    certificate_after_days: FormStr = "",
    is_active: FormStr = "",
    rule_grade: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    rule_months: FormList = [],  # noqa: B006
    rule_days: FormList = [],  # noqa: B006
) -> Response:
    try:
        with db.begin_nested():
            settings.save_type(
                db,
                actor,
                _uuid(type_id),
                name=name,
                paid=bool(paid),
                counting=counting,
                has_balance=bool(has_balance),
                accrual=accrual,
                carry_over_max_days=carry_over_max_days,
                carry_over_expiry_month=carry_over_expiry_month,
                requires_hr_approval=bool(requires_hr_approval),
                certificate_after_days=certificate_after_days,
                is_active=bool(is_active) or not type_id,
                rules=list(zip(rule_grade, rule_months, rule_days, strict=False)),
            )
    except (ValidationFailed, Conflict) as exc:
        context = _settings_context(
            db,
            actor,
            clock.today().year,
            errors=_errors(exc),
            editing=f"type:{type_id or 'new'}",
        )
        return _render(request, "leave/settings.html", context, 422)
    return _see_other("/leave/settings?notice=1#types")


@router.post("/leave/settings/holidays", response_class=HTMLResponse)
@requires("leave.settings.manage")
@screen("leave.settings")
def add_holiday(
    request: Request, db: Db, actor: CurrentActor, day: FormStr = "", holiday_name: FormStr = ""
) -> Response:
    when = parse_day(day)
    try:
        with db.begin_nested():
            settings.add_holiday(db, actor, day=when, name=holiday_name)
    except ValidationFailed as exc:
        context = _settings_context(
            db,
            actor,
            when.year if when else clock.today().year,
            errors=_errors(exc),
            editing="holiday",
        )
        return _render(request, "leave/settings.html", context, 422)
    year = when.year if when else clock.today().year
    return _see_other(f"/leave/settings?year={year}&notice=1#holidays")


@router.post("/leave/settings/holidays/{holiday_id}/delete", response_class=HTMLResponse)
@requires("leave.settings.manage")
@screen("leave.settings")
def delete_holiday(db: Db, actor: CurrentActor, holiday_id: uuid.UUID) -> Response:
    settings.delete_holiday(db, actor, holiday_id)
    return _see_other("/leave/settings?notice=1#holidays")
