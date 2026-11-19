"""The org resolver (ADR 0015): who holds a position, who manages whom, who reports to
whom and the approval chain, as of a date. Approval routing and the `team` and
`department` scopes use only this module; nothing else walks the position tree.

People are employee IDs here. The HR module maps sign-in accounts to employees.
"""

import logging
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import clock
from app.org.models import Department, OrgState, Position, PositionAssignment

log = logging.getLogger("app.org")

KIND_ORDER = {"primary": 0, "acting": 1, "interim": 2}


@dataclass(frozen=True, slots=True)
class Holding:
    position_id: uuid.UUID
    employee_id: uuid.UUID
    kind: str


@dataclass(slots=True)
class Snapshot:
    """The structure on one date. Built once per (org version, date) and then read-only."""

    on: date
    parent: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)
    department: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    children: dict[uuid.UUID | None, list[uuid.UUID]] = field(
        default_factory=lambda: defaultdict(list)
    )
    holder: dict[uuid.UUID, Holding] = field(default_factory=dict)
    # Every current assignment of each employee, effective or not, best kind first.
    assignments: dict[uuid.UUID, list[Holding]] = field(default_factory=lambda: defaultdict(list))
    department_parent: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)
    department_children: dict[uuid.UUID, list[uuid.UUID]] = field(
        default_factory=lambda: defaultdict(list)
    )


_CACHE: "OrderedDict[tuple[int, date], Snapshot]" = OrderedDict()
_CACHE_SIZE = 32


def org_version(db: Session) -> int:
    return db.scalar(select(OrgState.version)) or 0


_DIRTY = "org_structure_changed"


def bump_version(db: Session) -> None:
    state = db.scalars(select(OrgState).with_for_update()).one()
    state.version += 1
    # This session now sees uncommitted structure under a version number that another
    # transaction could reuse if this one rolls back, so its snapshots are never cached.
    db.info[_DIRTY] = True


def _build(db: Session, on: date) -> Snapshot:
    snap = Snapshot(on=on)
    for pid, parent_id, department_id in db.execute(
        select(Position.id, Position.parent_id, Position.department_id).where(Position.is_active)
    ).all():
        snap.parent[pid] = parent_id
        snap.department[pid] = department_id
    for pid, parent_id in snap.parent.items():
        # A parent that is inactive (or missing) makes this position a root.
        snap.children[parent_id if parent_id in snap.parent else None].append(pid)
    rows = list(
        db.execute(
            select(
                PositionAssignment.position_id,
                PositionAssignment.employee_id,
                PositionAssignment.kind,
                PositionAssignment.start_date,
            ).where(
                PositionAssignment.start_date <= on,
                or_(PositionAssignment.end_date.is_(None), PositionAssignment.end_date >= on),
            )
        ).all()
    )
    # Best kind first; within a kind, the most recent start wins.
    rows.sort(key=lambda r: (KIND_ORDER[r.kind], -r.start_date.toordinal()))
    for pid, eid, kind, _ in rows:
        if pid not in snap.parent:
            continue
        holding = Holding(pid, eid, kind)
        snap.assignments[eid].append(holding)
        snap.holder.setdefault(pid, holding)
    for did, parent_id in db.execute(select(Department.id, Department.parent_id)).all():
        snap.department_parent[did] = parent_id
        if parent_id is not None:
            snap.department_children[parent_id].append(did)
    return snap


def snapshot(db: Session, on: date | None = None) -> Snapshot:
    on = on or clock.today()
    if db.info.get(_DIRTY):
        return _build(db, on)
    key = (org_version(db), on)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    snap = _build(db, on)
    _CACHE[key] = snap
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return snap


def _cycle(where: str, start: uuid.UUID) -> None:
    log.error("org_cycle_detected", extra={"where": where, "start": str(start)})


# ------------------------------------------------------------------ positions


def holder(db: Session, position_id: uuid.UUID, on: date | None = None) -> Holding | None:
    """The primary holder, else acting, else interim; None when vacant."""
    return snapshot(db, on).holder.get(position_id)


def ancestors(snap: Snapshot, position_id: uuid.UUID) -> list[uuid.UUID]:
    """Positions above `position_id`, nearest first. Stops at a cycle."""
    result: list[uuid.UUID] = []
    seen = {position_id}
    current = snap.parent.get(position_id)
    while current is not None and current in snap.parent:
        if current in seen:
            _cycle("positions", position_id)
            break
        seen.add(current)
        result.append(current)
        current = snap.parent.get(current)
    return result


def descendants(snap: Snapshot, position_id: uuid.UUID) -> list[uuid.UUID]:
    """Positions below `position_id`, breadth first. Stops at a cycle."""
    result: list[uuid.UUID] = []
    seen = {position_id}
    queue = list(snap.children.get(position_id, []))
    while queue:
        current = queue.pop(0)
        if current in seen:
            _cycle("positions", position_id)
            continue
        seen.add(current)
        result.append(current)
        queue.extend(snap.children.get(current, []))
    return result


# ------------------------------------------------------------------ people


def home_position(snap: Snapshot, employee_id: uuid.UUID) -> uuid.UUID | None:
    """Where the employee sits for reporting: their primary position, else their acting,
    else their interim one."""
    held = snap.assignments.get(employee_id) or []
    return held[0].position_id if held else None


def held_positions(snap: Snapshot, employee_id: uuid.UUID) -> list[uuid.UUID]:
    """Positions the employee holds as the effective holder (acting and interim count
    only where there is no primary holder)."""
    return [
        h.position_id
        for h in snap.assignments.get(employee_id) or []
        if snap.holder.get(h.position_id) == h
    ]


def manager_of(db: Session, employee_id: uuid.UUID, on: date | None = None) -> uuid.UUID | None:
    """The first person above the employee's home position, skipping vacant positions
    and positions the employee holds themselves."""
    chain = approval_chain(db, employee_id, on, levels=1)
    return chain[0] if chain else None


def approval_chain(
    db: Session, employee_id: uuid.UUID, on: date | None = None, *, levels: int | None = None
) -> list[uuid.UUID]:
    """Managers above the employee, nearest first, without repeats or the employee."""
    snap = snapshot(db, on)
    start = home_position(snap, employee_id)
    if start is None:
        return []
    chain: list[uuid.UUID] = []
    for pid in ancestors(snap, start):
        h = snap.holder.get(pid)
        if h is None or h.employee_id == employee_id or h.employee_id in chain:
            continue
        chain.append(h.employee_id)
        if levels is not None and len(chain) >= levels:
            break
    return chain


def reports_of(
    db: Session, employee_id: uuid.UUID, on: date | None = None, *, direct: bool = False
) -> set[uuid.UUID]:
    """Everyone who reports to the employee through the positions they hold. Direct
    reports are the holders of the nearest occupied positions below; vacancies are
    looked through."""
    snap = snapshot(db, on)
    result: set[uuid.UUID] = set()
    for top in held_positions(snap, employee_id):
        if not direct:
            for pid in descendants(snap, top):
                h = snap.holder.get(pid)
                if h is not None:
                    result.add(h.employee_id)
            continue
        seen = {top}
        queue = list(snap.children.get(top, []))
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                _cycle("positions", top)
                continue
            seen.add(pid)
            h = snap.holder.get(pid)
            if h is not None and h.employee_id != employee_id:
                result.add(h.employee_id)
            else:
                queue.extend(snap.children.get(pid, []))
    result.discard(employee_id)
    return result


# ------------------------------------------------------------------ departments


def departments_of(db: Session, employee_id: uuid.UUID, on: date | None = None) -> set[uuid.UUID]:
    """Departments of the positions the employee holds."""
    snap = snapshot(db, on)
    return {snap.department[pid] for pid in held_positions(snap, employee_id)}


def departments_under(db: Session, department_id: uuid.UUID) -> set[uuid.UUID]:
    """A department and every department below it. Stops at a cycle."""
    snap = snapshot(db)
    result = {department_id}
    queue = list(snap.department_children.get(department_id, []))
    while queue:
        current = queue.pop(0)
        if current in result:
            _cycle("departments", department_id)
            continue
        result.add(current)
        queue.extend(snap.department_children.get(current, []))
    return result


def department_employees(
    db: Session, department_ids: set[uuid.UUID], on: date | None = None
) -> set[uuid.UUID]:
    """Effective holders of positions in these departments."""
    snap = snapshot(db, on)
    return {
        h.employee_id for pid, h in snap.holder.items() if snap.department[pid] in department_ids
    }


def department_ancestors(snap: Snapshot, department_id: uuid.UUID) -> list[uuid.UUID]:
    result: list[uuid.UUID] = []
    seen = {department_id}
    current = snap.department_parent.get(department_id)
    while current is not None:
        if current in seen:
            _cycle("departments", department_id)
            break
        seen.add(current)
        result.append(current)
        current = snap.department_parent.get(current)
    return result


def clear_cache() -> None:
    """For tests that change the structure without bumping the version."""
    _CACHE.clear()
