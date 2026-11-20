"""Leave requests (ADR 0018): asking, approving (manager step, then HR when the type
requires it), cancelling, certificates, and the lists built on them."""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.formatting import format_days
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.leave import balances, days
from app.modules.leave.files import LEAVE_CERTIFICATE
from app.modules.leave.models import LeaveDecision, LeaveRequest, LeaveType
from app.modules.leave.schemas import CalendarRow, DecisionView, RequestView
from app.org import resolver as org_resolver
from app.org import service as org

MIN_NOTE = 5


def _resource(employee_id: uuid.UUID) -> Resource:
    return Resource(owner_employee_id=employee_id)


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = _resource(employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def active_types(db: Session) -> list[LeaveType]:
    return list(
        db.scalars(select(LeaveType).where(LeaveType.is_active).order_by(LeaveType.sort_order))
    )


def _type(db: Session, type_id: uuid.UUID | None) -> LeaveType:
    leave_type = db.get(LeaveType, type_id) if type_id else None
    if leave_type is None or not leave_type.is_active:
        raise ValidationFailed(field_errors={"leave_type_id": "Choose a type of leave."})
    return leave_type


def needs_certificate(leave_type: LeaveType, request_days: Decimal) -> bool:
    limit = leave_type.certificate_after_days
    return limit is not None and request_days > limit


def preview(
    db: Session,
    employee_id: uuid.UUID,
    leave_type: LeaveType,
    start: date,
    end: date,
    *,
    start_half: bool = False,
    end_half: bool = False,
) -> tuple[Decimal, balances.Balance]:
    """Days the request would use and the balance it comes from."""
    used = days.count(
        db,
        start,
        end,
        counting=leave_type.counting,
        start_half=start_half,
        end_half=end_half,
    )
    return used, balances.balance(db, employee_id, leave_type, start.year)


# ------------------------------------------------------------------ requesting


def _check_overlap(
    db: Session, employee_id: uuid.UUID, start: date, end: date, own: uuid.UUID | None = None
) -> None:
    clash = db.scalar(
        select(LeaveRequest.id).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(("pending", "approved")),
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
            *([LeaveRequest.id != own] if own else []),
        )
    )
    if clash:
        raise ValidationFailed(
            field_errors={"start_date": "You already have leave booked on some of these days."}
        )


def request_leave(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    leave_type_id: uuid.UUID | None,
    start: date | None,
    end: date | None,
    start_half: bool = False,
    end_half: bool = False,
    reason: str = "",
) -> uuid.UUID:
    if not _can(db, actor, "leave.request.create", employee_id):
        raise PermissionDenied()
    facts = people.status_and_account(db, employee_id)
    if facts is None:
        raise NotFound()
    if facts[0] == "terminated":
        raise Conflict("This person has left the organisation.")
    leave_type = _type(db, leave_type_id)
    if start is None or end is None:
        raise ValidationFailed(
            field_errors={
                **({"start_date": "Choose the first day."} if start is None else {}),
                **({"end_date": "Choose the last day."} if end is None else {}),
            }
        )
    used, balance = preview(
        db, employee_id, leave_type, start, end, start_half=start_half, end_half=end_half
    )
    _check_overlap(db, employee_id, start, end)
    if leave_type.has_balance and used > balance.available:
        available = max(balance.available, Decimal(0))
        raise ValidationFailed(
            field_errors={
                "end_date": f"This needs {format_days(used)} days and "
                f"{format_days(available)} are available."
            }
        )
    manager = org.manager_of(db, employee_id, clock.today())
    request = LeaveRequest(
        employee_id=employee_id,
        leave_type_id=leave_type.id,
        start_date=start,
        end_date=end,
        start_half=start_half,
        end_half=end_half,
        days=used,
        year=start.year,
        reason=reason.strip() or None,
        step="manager" if manager else "hr",
        manager_employee_id=manager,
        requested_by=actor.user_id,
    )
    db.add(request)
    db.flush()
    record_by(
        db,
        actor,
        "leave.request.create",
        entity_type="leave_request",
        entity_id=str(request.id),
        changes=diff_changes(
            {},
            {
                "type": leave_type.name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": str(used),
            },
        ),
    )
    return request.id


def attach_certificate(
    db: Session,
    actor: Actor,
    request_id: uuid.UUID,
    *,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
) -> None:
    request = db.get(LeaveRequest, request_id)
    if request is None or not _can(db, actor, "leave.request.read", request.employee_id):
        raise NotFound()
    if request.status != "pending":
        raise Conflict("This request has already been decided.")
    file_id = files.upload(
        db,
        actor,
        purpose_key=LEAVE_CERTIFICATE.key,
        entity_id=str(request.id),
        filename=filename,
        declared_mime=declared_mime,
        data=data,
        replaces=request.certificate_file_id,
    )
    request.certificate_file_id = file_id
    record_by(
        db,
        actor,
        "leave.request.certificate",
        entity_type="leave_request",
        entity_id=str(request.id),
    )


# ------------------------------------------------------------------ deciding


def _owner_user(db: Session, request: LeaveRequest) -> uuid.UUID | None:
    facts = people.status_and_account(db, request.employee_id)
    return facts[1] if facts else None


def _step_permission(db: Session, actor: Actor, request: LeaveRequest) -> str:
    """The permission the actor would decide this step with. HR can always step in on the
    manager step (e.g. a manager without the Line Manager role)."""
    if request.step == "manager" and _can(db, actor, "leave.request.approve", request.employee_id):
        return "leave.request.approve"
    return "leave.request.approve_hr"


def may_decide(db: Session, actor: Actor, request: LeaveRequest) -> bool:
    if request.status != "pending":
        return False
    permission = _step_permission(db, actor, request)
    if not _can(db, actor, permission, request.employee_id):
        return False
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        permission,
        actor.user_id,
        {"owner_user_id": _owner_user(db, request)},
        held,
    )


def decide(
    db: Session, actor: Actor, request_id: uuid.UUID, *, approve: bool, note: str = ""
) -> None:
    request = db.scalars(
        select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update()
    ).one_or_none()
    if request is None:
        raise NotFound()
    permission = _step_permission(db, actor, request)
    visible = _can(db, actor, "leave.request.read", request.employee_id) or _can(
        db, actor, permission, request.employee_id
    )
    if not visible:
        raise NotFound()
    if request.status != "pending":
        raise Conflict("This request has already been decided.")
    if not may_decide(db, actor, request):
        if _can(db, actor, permission, request.employee_id):
            raise PermissionDenied("You can't approve your own leave.")
        raise PermissionDenied()
    note = note.strip()
    leave_type = db.get(LeaveType, request.leave_type_id)
    if leave_type is None:
        raise NotFound()
    if not approve and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, so they know what to change."})
    if approve and needs_certificate(leave_type, request.days) and not request.certificate_file_id:
        raise Conflict("Waiting for the medical certificate.")
    step = request.step
    db.add(
        LeaveDecision(
            request_id=request.id,
            step=step,
            decision="approved" if approve else "rejected",
            decided_by=actor.user_id,
            note=note or None,
        )
    )
    if not approve:
        request.status = "rejected"
    elif step == "manager" and leave_type.requires_hr_approval:
        request.step = "hr"
    else:
        request.status = "approved"
    if request.status != "pending":
        request.decided_at = clock.now()
    record_by(
        db,
        actor,
        f"leave.request.{'approve' if approve else 'reject'}",
        entity_type="leave_request",
        entity_id=str(request.id),
        reason=note or None,
        changes={
            "step": {"from": step, "to": request.step},
            "status": {"from": "pending", "to": request.status},
        },
    )


def cancel(db: Session, actor: Actor, request_id: uuid.UUID) -> None:
    request = db.scalars(
        select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update()
    ).one_or_none()
    if request is None or not _can(db, actor, "leave.request.read", request.employee_id):
        raise NotFound()
    if not can_cancel(db, actor, request):
        if request.status in {"rejected", "cancelled"}:
            raise Conflict("This request is already closed.")
        raise PermissionDenied("Leave that has started can only be cancelled by HR.")
    before = request.status
    request.status, request.cancelled_at, request.cancelled_by = (
        "cancelled",
        clock.now(),
        actor.user_id,
    )
    record_by(
        db,
        actor,
        "leave.request.cancel",
        entity_type="leave_request",
        entity_id=str(request.id),
        changes={"status": {"from": before, "to": "cancelled"}},
    )


def can_cancel(db: Session, actor: Actor, request: LeaveRequest) -> bool:
    if request.status not in {"pending", "approved"}:
        return False
    if _can(db, actor, "leave.request.cancel", request.employee_id):
        return True
    may_request = _can(db, actor, "leave.request.create", request.employee_id)
    return may_request and (request.status == "pending" or request.start_date > clock.today())


# ------------------------------------------------------------------ views


def _views(db: Session, actor: Actor, rows: list[LeaveRequest]) -> list[RequestView]:
    if not rows:
        return []
    types = {t.id: t for t in db.scalars(select(LeaveType))}
    ids = {r.employee_id for r in rows} | {
        r.manager_employee_id for r in rows if r.manager_employee_id
    }
    names = people.names(db, ids)
    held = org.positions_held(db, list({r.employee_id for r in rows}))
    decisions: dict[uuid.UUID, list[LeaveDecision]] = defaultdict(list)
    for d in db.scalars(
        select(LeaveDecision)
        .where(LeaveDecision.request_id.in_([r.id for r in rows]))
        .order_by(LeaveDecision.decided_at)
    ):
        decisions[d.request_id].append(d)
    deciders = {
        u.id: u.full_name
        for u in db.scalars(
            select(User).where(User.id.in_({d.decided_by for ds in decisions.values() for d in ds}))
        )
    }
    result = []
    for r in rows:
        t = types[r.leave_type_id]
        positions = held.get(r.employee_id, [])
        result.append(
            RequestView(
                id=r.id,
                employee_id=r.employee_id,
                employee_name=names.get(r.employee_id, ""),
                position=positions[0][0] if positions else None,
                leave_type_id=t.id,
                type_name=t.name,
                type_code=t.code,
                start_date=r.start_date,
                end_date=r.end_date,
                start_half=r.start_half,
                end_half=r.end_half,
                days=r.days,
                reason=r.reason,
                status=r.status,
                step=r.step,
                needs_hr=t.requires_hr_approval,
                manager_name=names.get(r.manager_employee_id) if r.manager_employee_id else None,
                certificate_needed=needs_certificate(t, r.days),
                certificate_file_id=r.certificate_file_id,
                created_at=r.created_at,
                decisions=[
                    DecisionView(
                        d.step, d.decision, deciders.get(d.decided_by), d.decided_at, d.note
                    )
                    for d in decisions[r.id]
                ],
                can_decide=may_decide(db, actor, r),
                can_cancel=can_cancel(db, actor, r),
            )
        )
    return result


def my_requests(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[RequestView]:
    rows = list(
        db.scalars(
            select(LeaveRequest)
            .where(LeaveRequest.employee_id == employee_id)
            .order_by(LeaveRequest.start_date.desc())
            .limit(50)
        )
    )
    return _views(db, actor, rows)


def get_request(db: Session, actor: Actor, request_id: uuid.UUID) -> RequestView:
    request = db.get(LeaveRequest, request_id)
    if request is None:
        raise NotFound()
    if not _can(db, actor, "leave.request.read", request.employee_id) and not may_decide(
        db, actor, request
    ):
        raise NotFound()
    return _views(db, actor, [request])[0]


def inbox(db: Session, actor: Actor) -> list[RequestView]:
    """Pending requests the actor may decide now."""
    clauses = []
    if engine.check(db, actor.user_id, "leave.request.approve"):
        clauses.append(
            and_(
                LeaveRequest.step == "manager",
                engine.scope_filter(db, actor.user_id, "leave.request.approve", LeaveRequest),
            )
        )
    if engine.check(db, actor.user_id, "leave.request.approve_hr"):
        clauses.append(LeaveRequest.step == "hr")
    if not clauses:
        return []
    rows = list(
        db.scalars(
            select(LeaveRequest)
            .where(LeaveRequest.status == "pending", or_(*clauses))
            .order_by(LeaveRequest.start_date)
            .limit(200)
        )
    )
    return [v for v in _views(db, actor, rows) if v.can_decide]


def decided_by_me(db: Session, actor: Actor) -> list[RequestView]:
    ids = select(LeaveDecision.request_id).where(LeaveDecision.decided_by == actor.user_id)
    rows = list(
        db.scalars(
            select(LeaveRequest)
            .where(LeaveRequest.id.in_(ids))
            .order_by(LeaveRequest.start_date.desc())
            .limit(100)
        )
    )
    return _views(db, actor, rows)


def away_alongside(db: Session, actor: Actor, request: RequestView) -> list[RequestView]:
    """Others the actor can see who are away on any of the same days."""
    rows = list(
        db.scalars(
            select(LeaveRequest).where(
                LeaveRequest.status.in_(("pending", "approved")),
                LeaveRequest.employee_id != request.employee_id,
                LeaveRequest.start_date <= request.end_date,
                LeaveRequest.end_date >= request.start_date,
                engine.scope_filter(db, actor.user_id, "leave.request.read", LeaveRequest),
            )
        )
    )
    return _views(db, actor, rows)


def calendar(
    db: Session, actor: Actor, year: int, month: int, *, department: uuid.UUID | None = None
) -> list[CalendarRow]:
    first = date(year, month, 1)
    last = date(year + month // 12, month % 12 + 1, 1)
    stmt = select(LeaveRequest).where(
        LeaveRequest.status.in_(("pending", "approved")),
        LeaveRequest.start_date < last,
        LeaveRequest.end_date >= first,
        engine.scope_filter(db, actor.user_id, "leave.request.read", LeaveRequest),
    )
    if department is not None:
        members = org_resolver.department_employees(
            db, org.departments_under(db, department), clock.today()
        )
        stmt = stmt.where(LeaveRequest.employee_id.in_(members))
    rows = list(db.scalars(stmt.order_by(LeaveRequest.start_date)))
    types = {t.id: t for t in db.scalars(select(LeaveType))}
    names = people.names(db, {r.employee_id for r in rows})
    held = org.positions_held(db, list(names))
    bars: dict[uuid.UUID, list[tuple[int, int, str, str, str]]] = defaultdict(list)
    end_of_month = last.toordinal() - first.toordinal()
    for r in rows:
        t = types[r.leave_type_id]
        start_day = max(r.start_date, first).day
        end_day = min(r.end_date.toordinal() - first.toordinal() + 1, end_of_month)
        bars[r.employee_id].append((start_day, end_day, t.code, t.name, r.status))
    return sorted(
        (
            CalendarRow(
                eid,
                names.get(eid, ""),
                held.get(eid, [("", "")])[0][0] or None,
                bars[eid],
            )
            for eid in bars
        ),
        key=lambda row: row.name,
    )


# ------------------------------------------------------------------ for other modules


def holidays(db: Session, start: date, end: date) -> dict[date, str]:
    """Public holidays between two dates (inclusive), by day."""
    return days.holidays_between(db, start, end)


def approved_days(
    db: Session, employee_ids: list[uuid.UUID], start: date, end: date
) -> dict[tuple[uuid.UUID, date], tuple[str, str]]:
    """Approved leave by (person, day) between two dates: the type's name and the part of
    the day taken ("full", "afternoon" when it starts after midday, "morning" when it
    ends at midday). Weekends and holidays inside a request are included."""
    if not employee_ids:
        return {}
    names = dict(db.execute(select(LeaveType.id, LeaveType.name)).all())
    found: dict[tuple[uuid.UUID, date], tuple[str, str]] = {}
    for r in db.scalars(
        select(LeaveRequest).where(
            LeaveRequest.employee_id.in_(employee_ids),
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    ):
        day = max(r.start_date, start)
        while day <= min(r.end_date, end):
            part = "full"
            if day == r.start_date and r.start_half:
                part = "afternoon"
            elif day == r.end_date and r.end_half:
                part = "morning"
            found[(r.employee_id, day)] = (names.get(r.leave_type_id, ""), part)
            day = date.fromordinal(day.toordinal() + 1)
    return found
