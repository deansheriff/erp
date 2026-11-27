"""Weekly timesheets (ADR 0031). A member logs hours by day against an active project,
optionally a task. Each person has one sheet per project per week (from Monday).
Submitting sends it to the project's manager, or, for the manager's own time, to their
line manager; with neither, someone allowed to step in decides. A rejection needs a
reason and returns the week to be corrected."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.authz import service as authz
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.projects import common
from app.modules.projects.common import ZERO
from app.modules.projects.models import Member, Project, Task, TimeEntry, Timesheet
from app.org import service as org

STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Waiting for approval",
    "approved": "Approved",
    "rejected": "Rejected",
}
STATUS_TONES = {
    "draft": "neutral",
    "submitted": "warning",
    "approved": "success",
    "rejected": "danger",
}
DAY_LIMIT = Decimal(24)


def _mine(db: Session, actor: Actor) -> uuid.UUID:
    employee = common.me(db, actor)
    if employee is None or not common.can(db, actor, common.LOG):
        raise PermissionDenied("Timesheets are for people on the staff list.")
    return employee


def loggable(db: Session, employee_id: uuid.UUID) -> list[Project]:
    """Active projects the person manages or is a member of."""
    member_of = set(db.scalars(select(Member.project_id).where(Member.employee_id == employee_id)))
    return [
        p
        for p in db.scalars(
            select(Project).where(Project.status == "active").order_by(Project.code)
        )
        if p.id in member_of or p.manager_employee_id == employee_id
    ]


def _sheet(
    db: Session, project_id: uuid.UUID, employee_id: uuid.UUID, week: date
) -> Timesheet | None:
    return db.scalars(
        select(Timesheet).where(
            Timesheet.project_id == project_id,
            Timesheet.employee_id == employee_id,
            Timesheet.week_start == week,
        )
    ).first()


# ------------------------------------------------------------------ the week


@dataclass(frozen=True, slots=True)
class Cell:
    project_id: str
    task_id: str
    day: date
    hours: str


def _hours(text: str, key: str, errors: dict[str, str]) -> Decimal:
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        return ZERO
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal(-1)
    if (
        not value.is_finite()
        or value < 0
        or value > DAY_LIMIT
        or value != value.quantize(Decimal("0.25"))
    ):
        errors[key] = "Enter hours in quarters, like 1.5 or 7.75."
        return ZERO
    return value


def _parse_cells(
    db: Session, employee: uuid.UUID, week: date, cells: list[Cell], errors: dict[str, str]
) -> dict[uuid.UUID, dict[tuple[date, uuid.UUID | None], Decimal]]:
    """Hours by project, then by (day, task)."""
    projects = {p.id: p for p in loggable(db, employee)}
    tasks = {
        t.id: t.project_id for t in db.scalars(select(Task).where(Task.project_id.in_(projects)))
    }
    out: dict[uuid.UUID, dict[tuple[date, uuid.UUID | None], Decimal]] = defaultdict(dict)
    per_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for n, cell in enumerate(cells):
        key = f"cell_{n}"
        hours = _hours(cell.hours, key, errors)
        if not hours or key in errors:
            continue
        project = common.uuid_or_none(cell.project_id)
        task = common.uuid_or_none(cell.task_id)
        if project not in projects:
            errors[key] = "Choose one of your active projects."
        elif cell.task_id and tasks.get(task) != project:  # type: ignore[arg-type]
            errors[key] = "Choose a task on that project."
        elif not week <= cell.day < week + timedelta(days=7):
            errors[key] = "That day isn't in this week."
        else:
            assert project is not None  # noqa: S101
            slot = (cell.day, task)
            out[project][slot] = out[project].get(slot, ZERO) + hours
            per_day[cell.day] += hours
    for day, total in per_day.items():
        if total > DAY_LIMIT:
            errors["form"] = f"{day:%A} comes to {total} hours, more than a day."
    return out


def save_week(db: Session, actor: Actor, week: date, cells: list[Cell]) -> None:
    """Replace the person's hours for the week on projects whose sheet is still open
    (draft or rejected). Submitted and approved weeks can't change."""
    employee = _mine(db, actor)
    week = common.monday(week)
    errors: dict[str, str] = {}
    hours = _parse_cells(db, employee, week, cells, errors)
    locked = []
    for project_id in hours:
        found = _sheet(db, project_id, employee, week)
        if found is not None and found.status in ("submitted", "approved"):
            locked.append(project_id)
    if locked:
        errors["form"] = "Some of these hours are on a week already submitted or approved."
    if errors:
        raise ValidationFailed(field_errors=errors)
    for project in loggable(db, employee):
        sheet = _sheet(db, project.id, employee, week)
        if sheet is not None and sheet.status in ("submitted", "approved"):
            continue
        wanted = hours.get(project.id, {})
        if sheet is None and not wanted:
            continue
        if sheet is None:
            sheet = Timesheet(project_id=project.id, employee_id=employee, week_start=week)
            db.add(sheet)
            db.flush()
        for old in db.scalars(select(TimeEntry).where(TimeEntry.timesheet_id == sheet.id)):
            db.delete(old)
        db.flush()
        for (day, task), amount in wanted.items():
            db.add(TimeEntry(timesheet_id=sheet.id, day=day, task_id=task, hours=amount))
        db.flush()
    record_by(db, actor, "projects.time.save", entity_type="employee", entity_id=str(employee))


def _approver(db: Session, project: Project, employee: uuid.UUID) -> uuid.UUID | None:
    if employee != project.manager_employee_id:
        return project.manager_employee_id
    chain = org.approval_chain(db, employee, 1)
    return chain[0] if chain else None


def submit(db: Session, actor: Actor, project_id: uuid.UUID, week: date) -> str:
    """Send the week's hours on a project for approval. Returns who decides it."""
    employee = _mine(db, actor)
    sheet = _sheet(db, project_id, employee, common.monday(week))
    if sheet is None or sheet.status not in ("draft", "rejected"):
        raise Conflict("There's nothing to submit for that project this week.")
    project = common.project_of(db, project_id)
    if project.status != "active":
        raise Conflict("Time can be submitted only on an active project.")
    if not db.scalars(select(TimeEntry).where(TimeEntry.timesheet_id == sheet.id)).first():
        raise Conflict("Enter some hours first.")
    sheet.approver_employee_id = _approver(db, project, employee)
    sheet.status, sheet.submitted_at, sheet.note = "submitted", clock.now(), None
    db.flush()
    record_by(db, actor, "projects.time.submit", entity_type="timesheet", entity_id=str(sheet.id))
    if sheet.approver_employee_id is None:
        return "someone who can step in"
    return people.names(db, {sheet.approver_employee_id}).get(sheet.approver_employee_id, "")


# ------------------------------------------------------------------ deciding


def _owner_user(db: Session, sheet: Timesheet) -> uuid.UUID | None:
    return people.accounts_of(db, {sheet.employee_id}).get(sheet.employee_id)


def _clear(db: Session, actor: Actor, sheet: Timesheet, permission: str) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        permission,
        actor.user_id,
        {"owner_user_id": _owner_user(db, sheet)},
        held,
    )


def may_decide(db: Session, actor: Actor, sheet: Timesheet) -> bool:
    if sheet.status != "submitted" or _owner_user(db, sheet) == actor.user_id:
        return False
    if sheet.approver_employee_id is None:
        return common.can(db, actor, common.APPROVE_ANY) and _clear(
            db, actor, sheet, common.APPROVE_ANY
        )
    if not common.can(db, actor, common.APPROVE):
        return False
    named = people.accounts_of(db, {sheet.approver_employee_id}).get(sheet.approver_employee_id)
    return (
        named is not None
        and (named == actor.user_id or named in authz.delegators(db, actor.user_id, common.APPROVE))
        and _clear(db, actor, sheet, common.APPROVE)
    )


def decide(db: Session, actor: Actor, sheet_id: uuid.UUID, *, approve: bool, note: str) -> None:
    sheet = db.scalars(select(Timesheet).where(Timesheet.id == sheet_id).with_for_update()).first()
    if sheet is None:
        raise NotFound()
    if sheet.status != "submitted":
        raise Conflict("This timesheet isn't waiting for a decision.")
    if _owner_user(db, sheet) == actor.user_id:
        raise PermissionDenied("You can't approve your own time; your line manager does.")
    if not may_decide(db, actor, sheet):
        raise PermissionDenied("This timesheet is for the project's manager to decide.")
    if not approve and not note.strip():
        raise ValidationFailed(field_errors={f"note_{sheet.id}": "Say why it's rejected."})
    sheet.status = "approved" if approve else "rejected"
    sheet.decided_by, sheet.decided_at = actor.user_id, clock.now()
    sheet.note = note.strip() or None
    db.flush()
    record_by(
        db,
        actor,
        f"projects.time.{'approve' if approve else 'reject'}",
        entity_type="timesheet",
        entity_id=str(sheet.id),
    )


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class Row:
    """One grid row: a project and optionally a task, with hours by weekday."""

    project_id: uuid.UUID
    project: str
    task_id: uuid.UUID | None
    task: str | None
    hours: list[Decimal]  # Monday to Sunday
    locked: bool

    @property
    def total(self) -> Decimal:
        return sum(self.hours, ZERO)


@dataclass(frozen=True, slots=True)
class SheetState:
    project_id: uuid.UUID
    project: str
    status: str | None  # None: nothing logged yet
    hours: Decimal
    approver: str | None
    decided_by: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class Week:
    start: date
    days: list[date]
    rows: list[Row]
    sheets: list[SheetState]
    projects: list[tuple[uuid.UUID, str]]
    tasks: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = field(default_factory=dict)

    @property
    def day_totals(self) -> list[Decimal]:
        return [sum((r.hours[i] for r in self.rows), ZERO) for i in range(7)]

    @property
    def total(self) -> Decimal:
        return sum(self.day_totals, ZERO)


def week_view(db: Session, actor: Actor, week: date) -> Week:
    employee = _mine(db, actor)
    start = common.monday(week)
    days = [start + timedelta(days=i) for i in range(7)]
    projects = loggable(db, employee)
    sheets = list(
        db.scalars(
            select(Timesheet).where(
                Timesheet.employee_id == employee, Timesheet.week_start == start
            )
        )
    )
    by_project = {s.project_id: s for s in sheets}
    all_projects = {p.id: p for p in projects}
    for s in sheets:
        if s.project_id not in all_projects:
            all_projects[s.project_id] = common.project_of(db, s.project_id)
    task_rows = list(
        db.scalars(select(Task).where(Task.project_id.in_(all_projects)).order_by(Task.title))
    )
    task_names = {t.id: t.title for t in task_rows}
    grid: dict[tuple[uuid.UUID, uuid.UUID | None], list[Decimal]] = {}
    for s in sheets:
        for entry in db.scalars(select(TimeEntry).where(TimeEntry.timesheet_id == s.id)):
            hours = grid.setdefault((s.project_id, entry.task_id), [ZERO] * 7)
            hours[(entry.day - start).days] += entry.hours
    approvers = people.names(db, {s.approver_employee_id for s in sheets if s.approver_employee_id})
    deciders = common.user_names(db, {s.decided_by for s in sheets})
    label = {pid: f"{p.code} · {p.name}" for pid, p in all_projects.items()}
    rows = [
        Row(
            pid,
            label[pid],
            tid,
            task_names.get(tid) if tid else None,
            hours,
            by_project[pid].status in ("submitted", "approved") if pid in by_project else False,
        )
        for (pid, tid), hours in sorted(
            grid.items(), key=lambda kv: (label[kv[0][0]], str(kv[0][1]))
        )
    ]
    states = []
    for pid in sorted(all_projects, key=lambda i: label[i]):
        found = by_project.get(pid)
        states.append(
            SheetState(
                pid,
                label[pid],
                found.status if found else None,
                sum((r.total for r in rows if r.project_id == pid), ZERO),
                approvers.get(found.approver_employee_id)
                if found and found.approver_employee_id
                else None,
                deciders.get(found.decided_by) if found and found.decided_by else None,
                found.note if found else None,
            )
        )
    tasks: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
    for t in task_rows:
        if t.status != "done":
            tasks[t.project_id].append((t.id, t.title))
    return Week(
        start,
        days,
        rows,
        states,
        [(p.id, f"{p.code} · {p.name}") for p in projects],
        dict(tasks),
    )


@dataclass(frozen=True, slots=True)
class Pending:
    sheet: Timesheet
    person: str
    project: str
    hours: Decimal
    entries: list[tuple[date, str | None, Decimal]]


def pending(db: Session, actor: Actor) -> list[Pending]:
    """Submitted timesheets the actor may decide, oldest first."""
    sheets = [
        s
        for s in db.scalars(
            select(Timesheet)
            .where(Timesheet.status == "submitted")
            .order_by(Timesheet.submitted_at)
        )
        if may_decide(db, actor, s)
    ]
    if not sheets:
        return []
    names = people.names(db, {s.employee_id for s in sheets})
    projects = {
        p.id: p
        for p in db.scalars(select(Project).where(Project.id.in_({s.project_id for s in sheets})))
    }
    out = []
    for s in sheets:
        entries = list(
            db.execute(
                select(TimeEntry.day, Task.title, TimeEntry.hours)
                .outerjoin(Task, Task.id == TimeEntry.task_id)
                .where(TimeEntry.timesheet_id == s.id)
                .order_by(TimeEntry.day)
            ).all()
        )
        out.append(
            Pending(
                s,
                names.get(s.employee_id, ""),
                f"{projects[s.project_id].code} · {projects[s.project_id].name}",
                sum((e[2] for e in entries), ZERO),
                [(d, t, h) for d, t, h in entries],
            )
        )
    return out


def waiting_count(db: Session, actor: Actor) -> int:
    return len(pending(db, actor))
