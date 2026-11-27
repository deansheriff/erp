"""The organisation record (ADR 0002) and its structure: branches, cost centres,
grades, departments, positions and position assignments (ADR 0015).

Every structural change bumps the org version (so resolver snapshots refresh) and is
audited. People are employee IDs here; the HR module owns employees.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import service as authz
from app.core import clock
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.identity.schemas import Actor
from app.org import resolver
from app.org.models import (
    ASSIGNMENT_KINDS,
    Branch,
    CostCentre,
    Department,
    Grade,
    Organization,
    Position,
    PositionAssignment,
    ReferenceMixin,
)
from app.org.schemas import (
    BrandingInput,
    DepartmentRow,
    HolderView,
    OrganizationInput,
    OrganizationView,
    PositionDetail,
    PositionNode,
    ReferenceItem,
)

MIN_REASON = 5


def get_organization(db: Session) -> OrganizationView | None:
    row = db.scalars(select(Organization)).one_or_none()
    if row is None:
        return None
    return OrganizationView(
        legal_name=row.legal_name,
        trading_name=row.trading_name,
        rc_number=row.rc_number,
        tin=row.tin,
        address=row.address,
        timezone=row.timezone,
        base_currency=row.base_currency,
        fiscal_year_start_month=row.fiscal_year_start_month,
        brand_primary=row.brand_primary,
        brand_accent=row.brand_accent,
    )


def organization_id(db: Session) -> uuid.UUID | None:
    return db.scalar(select(Organization.id))


def save_organization(db: Session, data: OrganizationInput) -> None:
    row = db.scalars(select(Organization)).one_or_none()
    if row is None:
        row = Organization(legal_name=data.legal_name)
        db.add(row)
    row.legal_name = data.legal_name.strip()
    row.trading_name = data.trading_name
    row.rc_number = data.rc_number
    row.tin = data.tin
    row.address = data.address
    row.timezone = data.timezone
    row.base_currency = data.base_currency.upper()
    row.fiscal_year_start_month = data.fiscal_year_start_month
    db.flush()


def _details(row: Organization) -> dict[str, object]:
    return {
        "legal_name": row.legal_name,
        "trading_name": row.trading_name,
        "rc_number": row.rc_number,
        "tin": row.tin,
        "address": row.address,
        "timezone": row.timezone,
        "fiscal_year_start_month": row.fiscal_year_start_month,
        "brand_primary": row.brand_primary,
        "brand_accent": row.brand_accent,
    }


def save_details(
    db: Session, actor: Actor, data: OrganizationInput, branding: BrandingInput
) -> None:
    """Administration → Organisation. Base currency and timezone are fixed at setup:
    money and dates already recorded depend on them."""
    authz.require(db, actor, "system.branding.manage")
    row = db.scalars(select(Organization)).one()
    before = _details(row)
    save_organization(
        db, data.model_copy(update={"base_currency": row.base_currency, "timezone": row.timezone})
    )
    row.brand_primary, row.brand_accent = branding.brand_primary, branding.brand_accent
    changes = diff_changes(before, _details(row))
    if changes:
        record_by(
            db,
            actor,
            "org.details.update",
            entity_type="organization",
            entity_id=str(row.id),
            changes=changes,
        )


# ------------------------------------------------------------------ helpers


def _changed(db: Session) -> None:
    db.flush()
    resolver.bump_version(db)


def _clean_code(code: str) -> str:
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,19}", code):
        raise ValidationFailed(
            field_errors={"code": "Use up to 20 letters, digits, hyphens or underscores."}
        )
    return code


def _clean_name(name: str, field: str = "name") -> str:
    name = " ".join(name.split())
    if not 2 <= len(name) <= 120:
        raise ValidationFailed(field_errors={field: "Use between 2 and 120 characters."})
    return name


def _reason(reason: str) -> str:
    reason = reason.strip()
    if len(reason) < MIN_REASON:
        raise ValidationFailed(field_errors={"reason": "Say why, in a few words."})
    return reason


# ------------------------------------------------------------------ branches, cost centres, grades

ReferenceModel = type[ReferenceMixin]
REFERENCE_KINDS: dict[str, ReferenceModel] = {
    "branches": Branch,
    "cost_centres": CostCentre,
    "grades": Grade,
}
REFERENCE_ENTITY = {"branches": "branch", "cost_centres": "cost_centre", "grades": "grade"}


def _reference_usage(db: Session, kind: str) -> dict[uuid.UUID, int]:
    if kind == "cost_centres":
        column = Department.cost_centre_id
        stmt = select(column, func.count()).where(Department.is_active)
    else:
        column = Position.branch_id if kind == "branches" else Position.grade_id
        stmt = select(column, func.count()).where(Position.is_active)
    return {k: n for k, n in db.execute(stmt.group_by(column)).all() if k is not None}


def list_reference(db: Session, kind: str) -> list[ReferenceItem]:
    model = REFERENCE_KINDS[kind]
    usage = _reference_usage(db, kind)
    order = (Grade.rank.desc(), Grade.code) if model is Grade else (model.code,)
    return [
        ReferenceItem(
            id=r.id,
            code=r.code,
            name=r.name,
            is_active=r.is_active,
            rank=r.rank if isinstance(r, Grade) else None,
            address=r.address if isinstance(r, Branch) else None,
            state=r.state if isinstance(r, Branch) else None,
            usage=usage.get(r.id, 0),
        )
        for r in db.scalars(select(model).order_by(model.is_active.desc(), *order))
    ]


def save_reference(
    db: Session,
    actor: Actor,
    kind: str,
    item_id: uuid.UUID | None,
    *,
    code: str,
    name: str,
    rank: int | None = None,
    address: str | None = None,
    state: str | None = None,
) -> uuid.UUID:
    authz.require(db, actor, "org.structure.manage")
    model = REFERENCE_KINDS[kind]
    code, name = _clean_code(code), _clean_name(name)
    clash = db.scalar(select(model.id).where(model.code == code))
    if clash is not None and clash != item_id:
        raise ValidationFailed(field_errors={"code": "Another record already uses this code."})
    if model is Grade and (rank is None or not 0 <= rank <= 999):
        raise ValidationFailed(field_errors={"rank": "Enter a number from 0 to 999."})
    row: ReferenceMixin | None
    if item_id is None:
        row = model()
        db.add(row)
        before: dict[str, object] = {}
    else:
        row = db.get(model, item_id)
        if row is None:
            raise NotFound()
        before = {"code": row.code, "name": row.name}
    row.code, row.name = code, name
    after: dict[str, object] = {"code": code, "name": name}
    if isinstance(row, Grade) and rank is not None:
        before.setdefault("rank", row.rank if item_id else None)
        row.rank = after["rank"] = rank
    if isinstance(row, Branch):
        row.address = (address or "").strip() or None
        row.state = (state or "").strip() or None
    _changed(db)
    entity = REFERENCE_ENTITY[kind]
    record_by(
        db,
        actor,
        f"org.{entity}.{'update' if item_id else 'create'}",
        entity_type=entity,
        entity_id=str(row.id),
        changes=diff_changes(before, after),
    )
    return row.id


def set_reference_active(
    db: Session, actor: Actor, kind: str, item_id: uuid.UUID, *, active: bool
) -> None:
    authz.require(db, actor, "org.structure.manage")
    row = db.get(REFERENCE_KINDS[kind], item_id)
    if row is None:
        raise NotFound()
    if not active and _reference_usage(db, kind).get(item_id):
        raise Conflict("It's still in use. Change the records that use it first.")
    row.is_active = active
    _changed(db)
    entity = REFERENCE_ENTITY[kind]
    record_by(
        db,
        actor,
        f"org.{entity}.{'activate' if active else 'deactivate'}",
        entity_type=entity,
        entity_id=str(row.id),
    )


# ------------------------------------------------------------------ trees

T = Department | Position


def _tree_order(rows: list[T], emit: Callable[[T, int, bool], None]) -> None:
    """Visit rows parents-first, alphabetically (rows arrive sorted). Rows whose parent
    is missing are roots; a row is never visited twice, so a cycle can't loop."""
    ids = {r.id for r in rows}
    children: dict[uuid.UUID | None, list[T]] = defaultdict(list)
    for r in rows:
        children[r.parent_id if r.parent_id in ids else None].append(r)
    seen: set[uuid.UUID] = set()

    def walk(parent: uuid.UUID | None, depth: int) -> None:
        for r in children.get(parent, []):
            if r.id in seen:
                continue
            seen.add(r.id)
            emit(r, depth, bool(children.get(r.id)))
            walk(r.id, depth + 1)

    walk(None, 0)


# ------------------------------------------------------------------ departments


def list_departments(db: Session, *, include_inactive: bool = False) -> list[DepartmentRow]:
    rows = list(db.scalars(select(Department).order_by(Department.name)))
    centres = {c.id: f"{c.code} · {c.name}" for c in db.scalars(select(CostCentre))}
    positions = dict(
        db.execute(
            select(Position.department_id, func.count())
            .where(Position.is_active)
            .group_by(Position.department_id)
        ).all()
    )
    snap = resolver.snapshot(db)
    people: dict[uuid.UUID, int] = defaultdict(int)
    for pid in snap.holder:
        people[snap.department[pid]] += 1
    result: list[DepartmentRow] = []

    def emit(d: T, depth: int, has_children: bool) -> None:
        assert isinstance(d, Department)  # noqa: S101  (narrowing for the type checker)
        if d.is_active or include_inactive:
            result.append(
                DepartmentRow(
                    id=d.id,
                    code=d.code,
                    name=d.name,
                    parent_id=d.parent_id,
                    depth=depth,
                    cost_centre=centres.get(d.cost_centre_id) if d.cost_centre_id else None,
                    cost_centre_id=d.cost_centre_id,
                    is_active=d.is_active,
                    positions=positions.get(d.id, 0),
                    people=people.get(d.id, 0),
                    has_children=has_children,
                )
            )

    _tree_order(list(rows), emit)
    return result


def save_department(
    db: Session,
    actor: Actor,
    department_id: uuid.UUID | None,
    *,
    code: str,
    name: str,
    parent_id: uuid.UUID | None,
    cost_centre_id: uuid.UUID | None,
) -> uuid.UUID:
    authz.require(db, actor, "org.structure.manage")
    code, name = _clean_code(code), _clean_name(name)
    clash = db.scalar(select(Department.id).where(Department.code == code))
    if clash is not None and clash != department_id:
        raise ValidationFailed(field_errors={"code": "Another department already uses this code."})
    if parent_id is not None:
        parent = db.get(Department, parent_id)
        if parent is None or not parent.is_active:
            raise ValidationFailed(field_errors={"parent_id": "Choose an active department."})
        if department_id is not None and parent_id in resolver.departments_under(db, department_id):
            raise ValidationFailed(
                field_errors={
                    "parent_id": "A department can't sit inside one of its own sub-departments."
                }
            )
    if cost_centre_id is not None:
        centre = db.get(CostCentre, cost_centre_id)
        if centre is None or not centre.is_active:
            raise ValidationFailed(field_errors={"cost_centre_id": "Choose an active cost centre."})
    before: dict[str, object] = {}
    if department_id is None:
        row = Department(code=code, name=name)
        db.add(row)
    else:
        found = db.get(Department, department_id)
        if found is None:
            raise NotFound()
        row = found
        before = {
            "code": row.code,
            "name": row.name,
            "parent_id": row.parent_id,
            "cost_centre_id": row.cost_centre_id,
        }
    row.code, row.name, row.parent_id, row.cost_centre_id = code, name, parent_id, cost_centre_id
    _changed(db)
    record_by(
        db,
        actor,
        "org.department." + ("update" if department_id else "create"),
        entity_type="department",
        entity_id=str(row.id),
        changes=diff_changes(
            before,
            {"code": code, "name": name, "parent_id": parent_id, "cost_centre_id": cost_centre_id},
        ),
    )
    return row.id


def set_department_active(
    db: Session, actor: Actor, department_id: uuid.UUID, *, active: bool
) -> None:
    authz.require(db, actor, "org.structure.manage")
    row = db.get(Department, department_id)
    if row is None:
        raise NotFound()
    if not active:
        if db.scalar(
            select(func.count())
            .select_from(Department)
            .where(Department.parent_id == row.id, Department.is_active)
        ):
            raise Conflict("Move or deactivate its sub-departments first.")
        if db.scalar(
            select(func.count())
            .select_from(Position)
            .where(Position.department_id == row.id, Position.is_active)
        ):
            raise Conflict("Move or deactivate its positions first.")
    elif row.parent_id is not None:
        parent = db.get(Department, row.parent_id)
        if parent is not None and not parent.is_active:
            raise Conflict("Reactivate its parent department first.")
    row.is_active = active
    _changed(db)
    record_by(
        db,
        actor,
        "org.department." + ("activate" if active else "deactivate"),
        entity_type="department",
        entity_id=str(row.id),
    )


# ------------------------------------------------------------------ positions


def _holder_view(a: PositionAssignment, today: date) -> HolderView:
    if a.end_date is not None and a.end_date < today:
        status = "ended"
    elif a.start_date > today:
        status = "upcoming"
    else:
        status = "current"
    return HolderView(a.id, a.employee_id, a.kind, a.start_date, a.end_date, a.reason, status)


def _current_assignments(db: Session, today: date) -> list[PositionAssignment]:
    return list(
        db.scalars(
            select(PositionAssignment).where(
                PositionAssignment.start_date <= today,
                or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
            )
        )
    )


def list_positions(db: Session, *, include_inactive: bool = False) -> list[PositionNode]:
    """Positions as a tree in display order (each parent before its children)."""
    today = clock.today()
    snap = resolver.snapshot(db, today)
    rows = list(db.scalars(select(Position).order_by(Position.title)))
    departments = {d.id: d.name for d in db.scalars(select(Department))}
    grades = {g.id: g.code for g in db.scalars(select(Grade))}
    branches = {b.id: b.name for b in db.scalars(select(Branch))}
    current = {(a.position_id, a.employee_id, a.kind): a for a in _current_assignments(db, today)}
    active_children: dict[uuid.UUID, int] = defaultdict(int)
    for p in rows:
        if p.parent_id is not None and p.is_active:
            active_children[p.parent_id] += 1
    result: list[PositionNode] = []

    def emit(p: T, depth: int, has_children: bool) -> None:
        assert isinstance(p, Position)  # noqa: S101  (narrowing for the type checker)
        if not (p.is_active or include_inactive):
            return
        h = snap.holder.get(p.id)
        a = current.get((p.id, h.employee_id, h.kind)) if h else None
        team = {
            snap.holder[d].employee_id for d in resolver.descendants(snap, p.id) if d in snap.holder
        }
        if h:
            team.discard(h.employee_id)
        result.append(
            PositionNode(
                id=p.id,
                title=p.title,
                parent_id=p.parent_id,
                depth=depth,
                department_id=p.department_id,
                department=departments.get(p.department_id, "—"),
                grade=grades.get(p.grade_id) if p.grade_id else None,
                branch=branches.get(p.branch_id) if p.branch_id else None,
                is_active=p.is_active,
                holder=_holder_view(a, today) if a else None,
                direct_reports=active_children.get(p.id, 0),
                team_size=len(team),
                child_count=int(has_children),
            )
        )

    _tree_order(list(rows), emit)
    return result


def position_detail(db: Session, position_id: uuid.UUID) -> PositionDetail:
    position = db.get(Position, position_id)
    if position is None:
        raise NotFound()
    today = clock.today()
    nodes = {n.id: n for n in list_positions(db, include_inactive=True)}
    node = nodes.get(position_id)
    if node is None:
        raise NotFound()
    order = {"current": 0, "upcoming": 1, "ended": 2}
    history = sorted(
        (
            _holder_view(a, today)
            for a in db.scalars(
                select(PositionAssignment).where(PositionAssignment.position_id == position_id)
            )
        ),
        key=lambda h: (order[h.status], resolver.KIND_ORDER[h.kind], -h.start_date.toordinal()),
    )
    snap = resolver.snapshot(db, today)
    chain: list[tuple[str, uuid.UUID | None]] = []
    for pid in resolver.ancestors(snap, position_id):
        h = snap.holder.get(pid)
        chain.append((nodes[pid].title if pid in nodes else "—", h.employee_id if h else None))
    parent = db.get(Position, position.parent_id) if position.parent_id else None
    return PositionDetail(
        node=node,
        grade_id=position.grade_id,
        branch_id=position.branch_id,
        parent_title=parent.title if parent else None,
        history=history,
        children=[n for n in nodes.values() if n.parent_id == position_id and n.is_active],
        chain=chain,
    )


def save_position(
    db: Session,
    actor: Actor,
    position_id: uuid.UUID | None,
    *,
    title: str,
    department_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
    grade_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
) -> uuid.UUID:
    authz.require(db, actor, "org.structure.manage")
    title = _clean_name(title, "title")
    department = db.get(Department, department_id) if department_id else None
    if department is None or not department.is_active:
        raise ValidationFailed(field_errors={"department_id": "Choose an active department."})
    if parent_id is not None:
        parent = db.get(Position, parent_id)
        if parent is None or not parent.is_active:
            raise ValidationFailed(field_errors={"parent_id": "Choose an active position."})
        if position_id is not None and (
            parent_id == position_id
            or parent_id in resolver.descendants(resolver.snapshot(db), position_id)
        ):
            raise ValidationFailed(
                field_errors={"parent_id": "A position can't report to one of its own reports."}
            )
    choices: tuple[tuple[str, ReferenceModel, uuid.UUID | None], ...] = (
        ("grade_id", Grade, grade_id),
        ("branch_id", Branch, branch_id),
    )
    for field, model, value in choices:
        if value is not None:
            found = db.get(model, value)
            if found is None or not found.is_active:
                raise ValidationFailed(field_errors={field: "Choose an active option."})
    before: dict[str, object] = {}
    if position_id is None:
        row = Position(title=title, department_id=department.id)
        db.add(row)
    else:
        found_position = db.get(Position, position_id)
        if found_position is None:
            raise NotFound()
        row = found_position
        before = {
            "title": row.title,
            "department_id": row.department_id,
            "parent_id": row.parent_id,
            "grade_id": row.grade_id,
            "branch_id": row.branch_id,
        }
    row.title, row.department_id, row.parent_id = title, department.id, parent_id
    row.grade_id, row.branch_id = grade_id, branch_id
    _changed(db)
    record_by(
        db,
        actor,
        "org.position." + ("update" if position_id else "create"),
        entity_type="position",
        entity_id=str(row.id),
        changes=diff_changes(
            before,
            {
                "title": title,
                "department_id": department.id,
                "parent_id": parent_id,
                "grade_id": grade_id,
                "branch_id": branch_id,
            },
        ),
    )
    return row.id


def set_position_active(db: Session, actor: Actor, position_id: uuid.UUID, *, active: bool) -> None:
    authz.require(db, actor, "org.structure.manage")
    row = db.get(Position, position_id)
    if row is None:
        raise NotFound()
    if not active:
        if db.scalar(
            select(func.count())
            .select_from(Position)
            .where(Position.parent_id == row.id, Position.is_active)
        ):
            raise Conflict("Move or deactivate the positions that report to it first.")
        if db.scalar(
            select(func.count())
            .select_from(PositionAssignment)
            .where(
                PositionAssignment.position_id == row.id,
                or_(
                    PositionAssignment.end_date.is_(None),
                    PositionAssignment.end_date >= clock.today(),
                ),
            )
        ):
            raise Conflict("End its current and upcoming assignments first.")
    elif row.parent_id is not None:
        parent = db.get(Position, row.parent_id)
        if parent is not None and not parent.is_active:
            raise Conflict("Reactivate the position it reports to first.")
    row.is_active = active
    _changed(db)
    record_by(
        db,
        actor,
        "org.position." + ("activate" if active else "deactivate"),
        entity_type="position",
        entity_id=str(row.id),
    )


def departments_under(db: Session, department_id: uuid.UUID) -> set[uuid.UUID]:
    """A department and every department below it."""
    return resolver.departments_under(db, department_id)


@dataclass(frozen=True, slots=True)
class DepartmentPeople:
    """Who currently holds positions in a department (not counting sub-departments),
    and who holds its top positions (those whose parent sits outside it)."""

    department_id: uuid.UUID
    holders: frozenset[uuid.UUID]
    heads: frozenset[uuid.UUID]


def department_people(db: Session, on: date) -> dict[uuid.UUID, DepartmentPeople]:
    """Current holders by active department, for department chat channels (ADR 0034)."""
    positions = {p.id: p for p in db.scalars(select(Position).where(Position.is_active))}
    holders: dict[uuid.UUID, set[uuid.UUID]] = {}
    heads: dict[uuid.UUID, set[uuid.UUID]] = {}
    for a in _current_assignments(db, on):
        position = positions.get(a.position_id)
        if position is None:
            continue
        holders.setdefault(position.department_id, set()).add(a.employee_id)
        parent = positions.get(position.parent_id) if position.parent_id else None
        if parent is None or parent.department_id != position.department_id:
            heads.setdefault(position.department_id, set()).add(a.employee_id)
    return {
        d: DepartmentPeople(d, frozenset(holders.get(d, ())), frozenset(heads.get(d, ())))
        for d in db.scalars(select(Department.id).where(Department.is_active))
    }


def position_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    """Active positions with their department, for pickers, in tree order."""
    return [(n.id, f"{'— ' * n.depth}{n.title} · {n.department}") for n in list_positions(db)]


# ------------------------------------------------------------------ assignments


def _overlapping(start: date, end: date | None) -> list[ColumnElement[bool]]:
    """Assignments whose period overlaps [start, end] (both inclusive)."""
    clauses: list[ColumnElement[bool]] = [
        or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= start)
    ]
    if end is not None:
        clauses.append(PositionAssignment.start_date <= end)
    return clauses


def assign(
    db: Session,
    actor: Actor,
    *,
    position_id: uuid.UUID,
    employee_id: uuid.UUID,
    kind: str,
    start_date: date,
    end_date: date | None,
    reason: str,
) -> uuid.UUID:
    """Put an employee in a position. Callers go through the HR service, which checks
    the employee first."""
    authz.require(db, actor, "org.assignment.manage")
    errors: dict[str, str] = {}
    if kind not in ASSIGNMENT_KINDS:
        errors["kind"] = "Choose primary, acting or interim."
    if end_date is not None and end_date < start_date:
        errors["end_date"] = "The end can't be before the start."
    if errors:
        raise ValidationFailed(field_errors=errors)
    reason = _reason(reason)
    # Lock the position so two people can't be assigned to the same seat at once.
    position = db.scalars(
        select(Position).where(Position.id == position_id).with_for_update()
    ).one_or_none()
    if position is None or not position.is_active:
        raise NotFound()
    period = _overlapping(start_date, end_date)
    if db.scalar(
        select(func.count())
        .select_from(PositionAssignment)
        .where(
            PositionAssignment.position_id == position_id, PositionAssignment.kind == kind, *period
        )
    ):
        raise Conflict(
            f"This position already has a {kind} holder for some of that period. "
            "End that assignment first."
        )
    if kind == "primary" and db.scalar(
        select(func.count())
        .select_from(PositionAssignment)
        .where(
            PositionAssignment.employee_id == employee_id,
            PositionAssignment.kind == "primary",
            *period,
        )
    ):
        raise Conflict("This person already has a primary position for some of that period.")
    if db.scalar(
        select(func.count())
        .select_from(PositionAssignment)
        .where(
            PositionAssignment.employee_id == employee_id,
            PositionAssignment.position_id == position_id,
            *period,
        )
    ):
        raise Conflict("This person already holds this position for some of that period.")
    row = PositionAssignment(
        position_id=position_id,
        employee_id=employee_id,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        created_by=actor.user_id,
    )
    db.add(row)
    _changed(db)
    record_by(
        db,
        actor,
        "org.assignment.create",
        entity_type="position",
        entity_id=str(position_id),
        reason=reason,
        changes={
            "employee_id": {"from": None, "to": str(employee_id)},
            "kind": {"from": None, "to": kind},
            "start_date": {"from": None, "to": start_date.isoformat()},
            "end_date": {"from": None, "to": end_date.isoformat() if end_date else None},
        },
    )
    return row.id


def end_assignment(
    db: Session, actor: Actor, assignment_id: uuid.UUID, *, last_day: date, reason: str
) -> None:
    """End an assignment after `last_day` (inclusive). One that hasn't started yet is
    cancelled instead."""
    authz.require(db, actor, "org.assignment.manage")
    row = db.get(PositionAssignment, assignment_id)
    if row is None:
        raise NotFound()
    reason = _reason(reason)
    today = clock.today()
    if row.end_date is not None and row.end_date < today:
        raise Conflict("This assignment has already ended.")
    if row.start_date > today:
        record_by(
            db,
            actor,
            "org.assignment.cancel",
            entity_type="position",
            entity_id=str(row.position_id),
            reason=reason,
            changes={"employee_id": {"from": str(row.employee_id), "to": None}},
        )
        db.delete(row)
        _changed(db)
        return
    if last_day < row.start_date:
        raise ValidationFailed(
            field_errors={"last_day": "The last day can't be before it started."}
        )
    if row.end_date is not None and last_day > row.end_date:
        raise ValidationFailed(field_errors={"last_day": "It already ends earlier than that."})
    previous = row.end_date
    row.end_date = last_day
    _changed(db)
    record_by(
        db,
        actor,
        "org.assignment.end",
        entity_type="position",
        entity_id=str(row.position_id),
        reason=reason,
        changes={
            "end_date": {
                "from": previous.isoformat() if previous else None,
                "to": last_day.isoformat(),
            }
        },
    )


def end_assignments_for_exit(
    db: Session, employee_id: uuid.UUID, *, last_day: date, reason: str
) -> int:
    """The system ends every position of someone who has left, after `last_day`.
    Assignments starting later are cancelled."""
    rows = list(
        db.scalars(
            select(PositionAssignment).where(
                PositionAssignment.employee_id == employee_id,
                or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date > last_day),
            )
        )
    )
    for row in rows:
        if row.start_date > last_day:
            db.delete(row)
            action = "org.assignment.cancel"
            changes: dict[str, dict[str, str | None]] = {
                "employee_id": {"from": str(employee_id), "to": None}
            }
        else:
            previous = row.end_date
            row.end_date = last_day
            action = "org.assignment.end"
            changes = {
                "end_date": {
                    "from": previous.isoformat() if previous else None,
                    "to": last_day.isoformat(),
                }
            }
        record_by(
            db,
            None,
            action,
            entity_type="position",
            entity_id=str(row.position_id),
            reason=reason,
            changes=changes,
        )
    if rows:
        _changed(db)
    return len(rows)


def manager_of(db: Session, employee_id: uuid.UUID, on: date) -> uuid.UUID | None:
    return resolver.manager_of(db, employee_id, on)


def primary_position(db: Session, employee_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Today's best position (position ID, department ID) of an employee, for choosing a
    checklist template."""
    today = clock.today()
    row = db.execute(
        select(Position.id, Position.department_id)
        .join(PositionAssignment, PositionAssignment.position_id == Position.id)
        .where(
            PositionAssignment.employee_id == employee_id,
            PositionAssignment.start_date <= today,
            or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
        )
        .order_by(
            case(
                (PositionAssignment.kind == "primary", 0),
                (PositionAssignment.kind == "acting", 1),
                else_=2,
            )
        )
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def grades_of(db: Session, employee_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Today's grade of each employee: the grade of their best current position."""
    if not employee_ids:
        return {}
    today = clock.today()
    rows = db.execute(
        select(PositionAssignment.employee_id, PositionAssignment.kind, Position.grade_id)
        .join(Position, Position.id == PositionAssignment.position_id)
        .where(
            PositionAssignment.employee_id.in_(employee_ids),
            PositionAssignment.start_date <= today,
            or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
            Position.grade_id.is_not(None),
        )
    ).all()
    result: dict[uuid.UUID, uuid.UUID] = {}
    for eid, _kind, grade_id in sorted(rows, key=lambda r: resolver.KIND_ORDER[r.kind]):
        if grade_id is not None:
            result.setdefault(eid, grade_id)
    return result


def assignment_position(db: Session, assignment_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(
        select(PositionAssignment.position_id).where(PositionAssignment.id == assignment_id)
    )


def positions_held(
    db: Session, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[str, str]]]:
    """Today's (position title, kind) for each employee, best kind first, for lists."""
    if not employee_ids:
        return {}
    today = clock.today()
    rows = db.execute(
        select(PositionAssignment.employee_id, Position.title, PositionAssignment.kind)
        .join(Position, Position.id == PositionAssignment.position_id)
        .where(
            PositionAssignment.employee_id.in_(employee_ids),
            PositionAssignment.start_date <= today,
            or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
        )
    ).all()
    result: dict[uuid.UUID, list[tuple[str, str]]] = defaultdict(list)
    for eid, title, kind in sorted(rows, key=lambda r: resolver.KIND_ORDER[r.kind]):
        result[eid].append((title, kind))
    return dict(result)


def approval_chain(db: Session, employee_id: uuid.UUID, levels: int) -> list[uuid.UUID]:
    """Today's managers above an employee, nearest first."""
    return resolver.approval_chain(db, employee_id, clock.today(), levels=levels)


def cost_centre_of(db: Session, employee_id: uuid.UUID) -> uuid.UUID | None:
    """The cost centre of the department of the employee's best current position."""
    held = primary_position(db, employee_id)
    if held is None:
        return None
    return db.scalar(select(Department.cost_centre_id).where(Department.id == held[1]))


def cost_centre_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    return [
        (c.id, f"{c.code} · {c.name}")
        for c in db.scalars(
            select(CostCentre).where(CostCentre.is_active).order_by(CostCentre.code)
        )
    ]
