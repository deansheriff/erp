"""The colleague directory (brief 9.2): directory-class fields only, for everyone who holds
`hr.employee.read`, within its scope. People who have left are not listed."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.authz import engine
from app.core import clock
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.hr.models import Employee
from app.modules.hr.schemas import DirectoryEntry
from app.org.models import Department, Position, PositionAssignment
from app.org.resolver import KIND_ORDER

PAGE_SIZE = 24


@dataclass(frozen=True, slots=True)
class DirectoryPage:
    entries: list[DirectoryEntry]
    total: int
    page: int
    pages: int


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def department_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    return [
        (d.id, d.name)
        for d in db.scalars(
            select(Department).where(Department.is_active).order_by(Department.name)
        )
    ]


def _current_positions(
    db: Session, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str, uuid.UUID]]:
    """Each employee's best current position: (title, kind, department ID)."""
    today = clock.today()
    rows = db.execute(
        select(
            PositionAssignment.employee_id,
            Position.title,
            PositionAssignment.kind,
            Position.department_id,
        )
        .join(Position, Position.id == PositionAssignment.position_id)
        .where(
            PositionAssignment.employee_id.in_(employee_ids),
            PositionAssignment.start_date <= today,
            or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
        )
    ).all()
    best: dict[uuid.UUID, tuple[str, str, uuid.UUID]] = {}
    for eid, title, kind, department_id in sorted(rows, key=lambda r: KIND_ORDER[r.kind]):
        best.setdefault(eid, (title, kind, department_id))
    return best


def search(
    db: Session,
    actor: Actor,
    *,
    q: str = "",
    department: str = "",
    branch: str = "",
    page: int = 1,
) -> DirectoryPage:
    stmt = select(Employee).where(
        engine.scope_filter(db, actor.user_id, "hr.employee.read", Employee),
        Employee.status != "terminated",
    )
    if q.strip():
        like = f"%{q.strip().lower()}%"
        titles = (
            select(PositionAssignment.employee_id)
            .join(Position, Position.id == PositionAssignment.position_id)
            .where(func.lower(Position.title).like(like))
        )
        stmt = stmt.where(
            or_(
                func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                func.lower(func.coalesce(Employee.other_names, "")).like(like),
                func.lower(func.coalesce(Employee.work_email, "")).like(like),
                Employee.id.in_(titles),
            )
        )
    branch_id, department_id = _uuid(branch), _uuid(department)
    if branch_id is not None:
        stmt = stmt.where(Employee.branch_id == branch_id)
    if department_id is not None:
        today = clock.today()
        members = (
            select(PositionAssignment.employee_id)
            .join(Position, Position.id == PositionAssignment.position_id)
            .where(
                Position.department_id == department_id,
                PositionAssignment.start_date <= today,
                or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= today),
            )
        )
        stmt = stmt.where(Employee.id.in_(members))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    rows = list(
        db.scalars(
            stmt.order_by(Employee.first_name, Employee.last_name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    positions = _current_positions(db, [r.id for r in rows])
    departments = {d.id: d.name for d in db.scalars(select(Department))}
    branches = people.branch_names(db)
    entries = []
    for r in rows:
        held = positions.get(r.id)
        entries.append(
            DirectoryEntry(
                id=r.id,
                full_name=r.full_name,
                initials=(r.first_name[:1] + r.last_name[:1]).upper(),
                position=held[0] if held else None,
                position_kind=held[1] if held else None,
                department=departments.get(held[2]) if held else None,
                branch=branches.get(r.branch_id) if r.branch_id else None,
                work_email=r.work_email,
            )
        )
    return DirectoryPage(entries, total, page, pages)
