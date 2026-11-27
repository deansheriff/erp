"""Projects (ADR 0031): set up by a Project Manager with a client, dates, budget by
category, members and milestones; tasks under milestones; the status from planned to
closed. Members see their projects; Finance and Directors see them all."""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.projects import common, figures
from app.modules.projects.common import ZERO
from app.modules.projects.models import (
    CATEGORIES,
    TASK_STATUSES,
    BudgetLine,
    Member,
    Milestone,
    Project,
    Task,
    Timesheet,
)
from app.org import service as org

TASK_LABELS = {"todo": "To do", "in_progress": "In progress", "blocked": "Blocked", "done": "Done"}
TASK_TONES = {"todo": "neutral", "in_progress": "info", "blocked": "danger", "done": "success"}
MOVES = {
    "planned": ("active", "on_hold", "cancelled"),
    "active": ("on_hold", "completed", "cancelled"),
    "on_hold": ("active", "completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}
MOVE_LABELS = {
    "active": "Start",
    "on_hold": "Put on hold",
    "completed": "Complete",
    "cancelled": "Cancel project",
}


# ------------------------------------------------------------------ setting up


@dataclass(frozen=True, slots=True)
class ProjectInput:
    name: str
    customer_id: str
    manager_id: str
    start_date: date | None
    end_date: date | None
    cost_centre_id: str
    description: str
    budget: dict[str, str]  # by category
    members: list[str]
    milestones: list[tuple[str, date | None]] = field(default_factory=list)  # new ones


def _check(db: Session, data: ProjectInput, errors: dict[str, str]) -> None:
    if not data.name.strip():
        errors["name"] = "Name the project."
    if data.start_date is None:
        errors["start_date"] = "Enter the start date."
    if data.end_date and data.start_date and data.end_date < data.start_date:
        errors["end_date"] = "It can't end before it starts."
    customer = common.uuid_or_none(data.customer_id)
    if data.customer_id and customer not in dict(finance.customer_choices(db)):
        errors["customer_id"] = "Choose a customer, or none."
    staff = set(people.current_ids(db))
    if common.uuid_or_none(data.manager_id) not in staff:
        errors["manager_id"] = "Choose the project manager."
    if any(common.uuid_or_none(m) not in staff for m in data.members if m):
        errors["members"] = "Choose people on the staff list."
    centre = common.uuid_or_none(data.cost_centre_id)
    if data.cost_centre_id and centre not in dict(org.cost_centre_choices(db)):
        errors["cost_centre_id"] = "Choose an active cost centre, or none."
    for name, due in data.milestones:
        if due and data.start_date and due < data.start_date:
            errors["milestones"] = f"“{name}” is due before the project starts."


def save(db: Session, actor: Actor, project_id: uuid.UUID | None, data: ProjectInput) -> uuid.UUID:
    common.require(db, actor, common.CREATE)
    row = common.project_of(db, project_id, lock=True) if project_id else None
    if row is not None and not common.can_manage(db, actor, row):
        raise PermissionDenied("Only the project's manager changes it.")
    if row is not None and row.status in ("completed", "cancelled"):
        raise Conflict("A closed project can't be changed.")
    errors: dict[str, str] = {}
    _check(db, data, errors)
    budget = {
        c: common.parse_money(data.budget.get(c, ""), f"budget_{c}", errors) for c in CATEGORIES
    }
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.start_date is not None  # noqa: S101  (checked above)
    manager = common.uuid_or_none(data.manager_id)
    assert manager is not None  # noqa: S101
    if row is None:
        row = Project(
            code=numbering.next_number(db, "project"),
            name=data.name.strip()[:200],
            manager_employee_id=manager,
            start_date=data.start_date,
            created_by=actor.user_id,
        )
        db.add(row)
        db.flush()
    row.name, row.manager_employee_id = data.name.strip()[:200], manager
    row.customer_id = common.uuid_or_none(data.customer_id)
    row.start_date, row.end_date = data.start_date, data.end_date
    row.cost_centre_id = common.uuid_or_none(data.cost_centre_id)
    row.description = data.description.strip() or None
    _save_budget(db, row, budget)
    _save_members(db, row, {m for m in (common.uuid_or_none(x) for x in data.members) if m})
    for name, due in data.milestones:
        if name.strip():
            db.add(Milestone(project_id=row.id, name=name.strip()[:200], due_date=due))
    db.flush()
    record_by(
        db,
        actor,
        "projects.project.save",
        entity_type="project",
        entity_id=str(row.id),
        changes={"code": row.code, "budget": str(sum(budget.values(), ZERO))},
    )
    return row.id


def _save_budget(db: Session, row: Project, budget: dict[str, Decimal]) -> None:
    lines = {
        b.category: b for b in db.scalars(select(BudgetLine).where(BudgetLine.project_id == row.id))
    }
    for category, amount in budget.items():
        line = lines.get(category)
        if line is None:
            db.add(BudgetLine(project_id=row.id, category=category, amount=amount))
        else:
            line.amount = amount


def _save_members(db: Session, row: Project, wanted: set[uuid.UUID]) -> None:
    current = {
        m.employee_id: m for m in db.scalars(select(Member).where(Member.project_id == row.id))
    }
    logged = set(
        db.scalars(select(Timesheet.employee_id).where(Timesheet.project_id == row.id).distinct())
    )
    for employee_id, member in current.items():
        if employee_id not in wanted:
            if employee_id in logged:
                raise ValidationFailed(
                    field_errors={"members": "Someone who has logged time can't be removed."}
                )
            db.delete(member)
    for employee_id in wanted - set(current):
        db.add(Member(project_id=row.id, employee_id=employee_id))
    db.flush()


def move(db: Session, actor: Actor, project_id: uuid.UUID, status: str) -> None:
    """Change the status: planned → active, on hold, completed or cancelled."""
    row = common.project_of(db, project_id, lock=True)
    if not common.can_manage(db, actor, row):
        raise PermissionDenied("Only the project's manager changes its status.")
    if status not in MOVES[row.status]:
        raise Conflict(f"A {common.STATUS_LABELS[row.status].lower()} project can't go there.")
    if status in ("completed", "cancelled"):
        waiting = db.scalar(
            select(func.count())
            .select_from(Timesheet)
            .where(Timesheet.project_id == row.id, Timesheet.status == "submitted")
        )
        if waiting:
            raise Conflict("Decide the timesheets waiting for approval first.")
    before = row.status
    row.status = status
    db.flush()
    record_by(
        db,
        actor,
        "projects.project.status",
        entity_type="project",
        entity_id=str(row.id),
        changes={"status": {"from": before, "to": status}},
    )


# ------------------------------------------------------------------ milestones and tasks


def _managed(db: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    row = common.project_of(db, project_id, lock=True)
    if not common.can_manage(db, actor, row):
        raise PermissionDenied("Only the project's manager changes its plan.")
    if row.status in ("completed", "cancelled"):
        raise Conflict("A closed project can't be changed.")
    return row


def add_milestone(
    db: Session, actor: Actor, project_id: uuid.UUID, *, name: str, due: date | None
) -> None:
    row = _managed(db, actor, project_id)
    if not name.strip():
        raise ValidationFailed(field_errors={"milestone_name": "Name the milestone."})
    db.add(Milestone(project_id=row.id, name=name.strip()[:200], due_date=due))
    db.flush()


def toggle_milestone(db: Session, actor: Actor, milestone_id: uuid.UUID) -> uuid.UUID:
    milestone = db.get(Milestone, milestone_id)
    if milestone is None:
        raise NotFound()
    _managed(db, actor, milestone.project_id)
    milestone.done_at = None if milestone.done_at else clock.now()
    db.flush()
    return milestone.project_id


@dataclass(frozen=True, slots=True)
class TaskInput:
    title: str
    milestone_id: str
    assignee_id: str
    due_date: date | None
    status: str
    description: str


def save_task(
    db: Session, actor: Actor, project_id: uuid.UUID, task_id: uuid.UUID | None, data: TaskInput
) -> uuid.UUID:
    row = _managed(db, actor, project_id)
    task = db.get(Task, task_id) if task_id else None
    if task_id and (task is None or task.project_id != row.id):
        raise NotFound()
    errors: dict[str, str] = {}
    if not data.title.strip():
        errors["title"] = "Say what the task is."
    milestone = common.uuid_or_none(data.milestone_id)
    milestones = set(db.scalars(select(Milestone.id).where(Milestone.project_id == row.id)))
    if data.milestone_id and milestone not in milestones:
        errors["milestone_id"] = "Choose one of the project's milestones."
    assignee = common.uuid_or_none(data.assignee_id)
    if data.assignee_id and not common.on_team(db, row, assignee):
        errors["assignee_id"] = "Choose someone on the project."
    if data.status not in TASK_STATUSES:
        errors["status"] = "Choose a status."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if task is None:
        task = Task(project_id=row.id, title=data.title.strip()[:200], created_by=actor.user_id)
        db.add(task)
    task.title, task.milestone_id, task.assignee_employee_id = (
        data.title.strip()[:200],
        milestone,
        assignee,
    )
    task.due_date, task.status = data.due_date, data.status
    task.description = data.description.strip() or None
    db.flush()
    record_by(db, actor, "projects.task.save", entity_type="project", entity_id=str(row.id))
    return task.id


def set_task_status(db: Session, actor: Actor, task_id: uuid.UUID, status: str) -> uuid.UUID:
    """The assignee or the manager moves a task along."""
    task = db.get(Task, task_id)
    if task is None:
        raise NotFound()
    row = common.project_of(db, task.project_id)
    mine = task.assignee_employee_id is not None and task.assignee_employee_id == common.me(
        db, actor
    )
    if not (mine or common.can_manage(db, actor, row)):
        raise PermissionDenied("Only the assignee or the project's manager moves this task.")
    if status not in TASK_STATUSES:
        raise ValidationFailed(field_errors={"status": "Choose a status."})
    task.status = status
    db.flush()
    return row.id


def delete_task(db: Session, actor: Actor, task_id: uuid.UUID) -> uuid.UUID:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFound()
    row = _managed(db, actor, task.project_id)
    db.delete(task)
    db.flush()
    return row.id


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class ProjectRow:
    project: Project
    client: str | None
    manager: str
    tasks_done: int
    tasks: int
    used: int | None  # % of budget, when the viewer may see figures


def list_projects(db: Session, actor: Actor, tab: str) -> list[ProjectRow]:
    me = common.me(db, actor)
    query = select(Project).order_by(Project.code.desc())
    if tab == "active":
        query = query.where(Project.status == "active")
    rows = list(db.scalars(query))
    sees_all = common.can(db, actor, common.READ)
    if tab == "mine" or not sees_all:
        rows = [r for r in rows if common.on_team(db, r, me)]
    numbers = figures.figures(db, {r.id for r in rows})
    clients = finance.customer_names(db, {r.customer_id for r in rows if r.customer_id})
    names = people.names(db, {r.manager_employee_id for r in rows})
    counts = dict(
        db.execute(
            select(Task.project_id, func.count())
            .where(Task.project_id.in_([r.id for r in rows]))
            .group_by(Task.project_id)
        ).all()
    )
    done = dict(
        db.execute(
            select(Task.project_id, func.count())
            .where(Task.project_id.in_([r.id for r in rows]), Task.status == "done")
            .group_by(Task.project_id)
        ).all()
    )
    return [
        ProjectRow(
            r,
            clients.get(r.customer_id) if r.customer_id else None,
            names.get(r.manager_employee_id, ""),
            done.get(r.id, 0),
            counts.get(r.id, 0),
            numbers[r.id].used if (sees_all or me == r.manager_employee_id) else None,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class TaskView:
    task: Task
    assignee: str | None


@dataclass(frozen=True, slots=True)
class MilestoneView:
    milestone: Milestone
    tasks: list[TaskView]

    @property
    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.task.status == "done")


@dataclass(frozen=True, slots=True)
class ProjectView:
    project: Project
    client: str | None
    manager: str
    cost_centre: str | None
    members: list[tuple[uuid.UUID, str]]
    milestones: list[MilestoneView]
    loose_tasks: list[TaskView]  # not under a milestone
    figures: figures.Figures | None  # None when the viewer can't see figures
    postings: list[finance.ProjectPosting]
    moves: list[tuple[str, str]]
    can_manage: bool
    me: uuid.UUID | None


def get(db: Session, actor: Actor, project_id: uuid.UUID) -> ProjectView:
    row = common.project_of(db, project_id)
    if not common.can_see(db, actor, row):
        raise NotFound()
    members = sorted(common.member_ids(db, row.id) | {row.manager_employee_id})
    tasks = list(
        db.scalars(
            select(Task).where(Task.project_id == row.id).order_by(Task.due_date, Task.created_at)
        )
    )
    names = people.names(
        db, set(members) | {t.assignee_employee_id for t in tasks if t.assignee_employee_id}
    )
    views = {
        t.id: TaskView(t, names.get(t.assignee_employee_id) if t.assignee_employee_id else None)
        for t in tasks
    }
    milestones = [
        MilestoneView(m, [views[t.id] for t in tasks if t.milestone_id == m.id])
        for m in db.scalars(
            select(Milestone)
            .where(Milestone.project_id == row.id)
            .order_by(Milestone.due_date.nulls_last(), Milestone.name)
        )
    ]
    figures_ok = common.can_see_figures(db, actor, row)
    manage = common.can_manage(db, actor, row)
    clients = finance.customer_names(db, {row.customer_id} if row.customer_id else set())
    centres = dict(org.cost_centre_choices(db))
    return ProjectView(
        row,
        clients.get(row.customer_id) if row.customer_id else None,
        names.get(row.manager_employee_id, ""),
        centres.get(row.cost_centre_id) if row.cost_centre_id else None,
        [(m, names.get(m, "")) for m in members],
        milestones,
        [views[t.id] for t in tasks if t.milestone_id is None],
        figures.figures(db, {row.id})[row.id] if figures_ok else None,
        finance.project_postings(db, row.id) if figures_ok else [],
        [(s, MOVE_LABELS[s]) for s in MOVES[row.status]] if manage else [],
        manage,
        common.me(db, actor),
    )


def budget_of(db: Session, project_id: uuid.UUID) -> dict[str, Decimal]:
    return {
        b.category: b.amount
        for b in db.scalars(select(BudgetLine).where(BudgetLine.project_id == project_id))
    }


def task_of(db: Session, actor: Actor, task_id: uuid.UUID) -> tuple[ProjectView, Task]:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFound()
    return get(db, actor, task.project_id), task


def staff(db: Session) -> list[tuple[uuid.UUID, str]]:
    return [(e, n) for e, n, _ in people.assignable_people(db)]


@dataclass(frozen=True, slots=True)
class Link:
    """Finance's view of projects (finance.project_link.ProjectLink)."""

    def choices(self, db: Session) -> list[tuple[uuid.UUID, str]]:
        return [
            (p.id, f"{p.code} · {p.name}")
            for p in db.scalars(
                select(Project).where(Project.status == "active").order_by(Project.code)
            )
        ]

    def is_open(self, db: Session, project_id: uuid.UUID) -> bool:
        row = db.get(Project, project_id)
        return row is not None and row.status == "active"

    def labels(self, db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        return {
            p.id: f"{p.code} · {p.name}"
            for p in db.scalars(select(Project).where(Project.id.in_(ids)))
        }
