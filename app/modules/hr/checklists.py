"""Onboarding and offboarding checklists (ADR 0017). Templates per position, department or
default; each started checklist copies its template's tasks with due dates and fixes the
manager on the day it starts."""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.authz import service as authz
from app.core import clock
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.hr.models import (
    ASSIGNEES,
    CHECKLIST_KINDS,
    Checklist,
    ChecklistTask,
    ChecklistTemplate,
    ChecklistTemplateTask,
    Employee,
    EmployeeExit,
)
from app.modules.hr.schemas import (
    ChecklistView,
    TaskView,
    TemplateTaskView,
    TemplateView,
)
from app.org import service as org
from app.org.models import Department, Position

ASSIGNEE_LABELS = {"hr": "HR", "it": "IT", "manager": "Manager", "employee": "Employee"}
MAX_TASKS = 40
MAX_OFFSET = 365


def access_end(last_working_day: date) -> datetime:
    """Midnight in the organisation's timezone after the last working day (ADR 0017)."""
    tz = ZoneInfo(get_settings().timezone)
    return datetime.combine(last_working_day + timedelta(days=1), time.min, tz).astimezone(UTC)


# ------------------------------------------------------------------ templates


def _target(
    t: ChecklistTemplate, departments: dict[uuid.UUID, str], positions: dict[uuid.UUID, str]
) -> str:
    if t.position_id:
        return f"Position: {positions.get(t.position_id, '?')}"
    if t.department_id:
        return f"Department: {departments.get(t.department_id, '?')}"
    return "Default"


def list_templates(db: Session) -> list[TemplateView]:
    departments = {d.id: d.name for d in db.scalars(select(Department))}
    positions = {p.id: p.title for p in db.scalars(select(Position))}
    tasks: dict[uuid.UUID, list[TemplateTaskView]] = {}
    for task in db.scalars(
        select(ChecklistTemplateTask).order_by(ChecklistTemplateTask.sort_order)
    ):
        tasks.setdefault(task.template_id, []).append(
            TemplateTaskView(task.id, task.title, task.assignee, task.due_offset_days)
        )
    return [
        TemplateView(
            id=t.id,
            kind=t.kind,
            name=t.name,
            target=_target(t, departments, positions),
            department_id=t.department_id,
            position_id=t.position_id,
            is_active=t.is_active,
            tasks=tasks.get(t.id, []),
        )
        for t in db.scalars(
            select(ChecklistTemplate).order_by(ChecklistTemplate.kind, ChecklistTemplate.name)
        )
    ]


def _clean_tasks(
    tasks: Sequence[tuple[str, str, str]], errors: dict[str, str]
) -> list[tuple[str, str, int]]:
    cleaned = []
    for i, (raw_title, assignee, offset) in enumerate(tasks):
        title = " ".join(raw_title.split())
        if not title:
            continue
        if len(title) > 200:
            errors[f"task_{i}"] = "Keep each task under 200 characters."
        if assignee not in ASSIGNEES:
            errors[f"task_{i}"] = "Choose who does this task."
        try:
            days = int(offset or "0")
        except ValueError:
            days = 0
            errors[f"task_{i}"] = "The due day must be a whole number."
        if abs(days) > MAX_OFFSET:
            errors[f"task_{i}"] = "Keep the due day within a year."
        cleaned.append((title, assignee, days))
    if not cleaned:
        errors["tasks"] = "Add at least one task."
    if len(cleaned) > MAX_TASKS:
        errors["tasks"] = f"A checklist can have up to {MAX_TASKS} tasks."
    return cleaned


def save_template(
    db: Session,
    actor: Actor,
    template_id: uuid.UUID | None,
    *,
    kind: str,
    name: str,
    department_id: uuid.UUID | None,
    position_id: uuid.UUID | None,
    is_active: bool,
    tasks: Sequence[tuple[str, str, str]],
) -> uuid.UUID:
    authz.require(db, actor, "hr.settings.manage")
    errors: dict[str, str] = {}
    name = " ".join(name.split())
    if not 2 <= len(name) <= 120:
        errors["name"] = "Give the template a name."
    if kind not in CHECKLIST_KINDS:
        errors["kind"] = "Choose onboarding or offboarding."
    if department_id and position_id:
        errors["target"] = "Choose a department or a position, not both."
    if department_id and db.get(Department, department_id) is None:
        errors["target"] = "Choose a department."
    if position_id and db.get(Position, position_id) is None:
        errors["target"] = "Choose a position."
    cleaned = _clean_tasks(tasks, errors)
    if errors:
        raise ValidationFailed(field_errors=errors)
    if template_id is None:
        row = ChecklistTemplate(kind=kind)
        db.add(row)
    else:
        found = db.get(ChecklistTemplate, template_id)
        if found is None:
            raise NotFound()
        row = found
        if row.kind != kind:
            raise ValidationFailed(field_errors={"kind": "A template can't change its kind."})
        for old in db.scalars(
            select(ChecklistTemplateTask).where(ChecklistTemplateTask.template_id == row.id)
        ):
            db.delete(old)
    row.name, row.department_id, row.position_id, row.is_active = (
        name,
        department_id,
        position_id,
        is_active,
    )
    db.flush()
    for i, (title, assignee, days) in enumerate(cleaned):
        db.add(
            ChecklistTemplateTask(
                template_id=row.id,
                title=title,
                assignee=assignee,
                due_offset_days=days,
                sort_order=i,
            )
        )
    record_by(
        db,
        actor,
        "hr.checklist_template.save",
        entity_type="checklist_template",
        entity_id=str(row.id),
        changes=diff_changes({}, {"name": name, "tasks": len(cleaned), "active": is_active}),
    )
    return row.id


def choose_template(db: Session, kind: str, employee_id: uuid.UUID) -> ChecklistTemplate | None:
    """The most specific active template: the employee's position, then department, then
    the default."""
    active = list(
        db.scalars(
            select(ChecklistTemplate).where(
                ChecklistTemplate.kind == kind, ChecklistTemplate.is_active
            )
        )
    )
    held = org.primary_position(db, employee_id)
    if held is not None:
        position_id, department_id = held
        for found in (
            next((t for t in active if t.position_id == position_id), None),
            next((t for t in active if t.department_id == department_id), None),
        ):
            if found is not None:
                return found
    return next((t for t in active if t.position_id is None and t.department_id is None), None)


# ------------------------------------------------------------------ checklists


def start(
    db: Session,
    actor: Actor | None,
    employee_id: uuid.UUID,
    *,
    kind: str,
    anchor_date: date,
    exit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Start a checklist from the best template. Callers check permissions: HR starts
    onboarding (`hr.checklist.manage`), recording an exit starts offboarding."""
    if db.scalar(
        select(Checklist.id).where(
            Checklist.employee_id == employee_id,
            Checklist.kind == kind,
            Checklist.completed_at.is_(None),
            Checklist.cancelled_at.is_(None),
        )
    ):
        raise Conflict(f"{kind.capitalize()} is already under way for this person.")
    template = choose_template(db, kind, employee_id)
    if template is None:
        raise Conflict(f"There is no active {kind} template. Add one in HR settings.")
    checklist = Checklist(
        employee_id=employee_id,
        kind=kind,
        name=template.name,
        template_id=template.id,
        exit_id=exit_id,
        anchor_date=anchor_date,
        manager_employee_id=org.manager_of(db, employee_id, clock.today()),
        started_by=actor.user_id if actor else None,
    )
    db.add(checklist)
    db.flush()
    for task in db.scalars(
        select(ChecklistTemplateTask)
        .where(ChecklistTemplateTask.template_id == template.id)
        .order_by(ChecklistTemplateTask.sort_order)
    ):
        db.add(
            ChecklistTask(
                checklist_id=checklist.id,
                title=task.title,
                assignee=task.assignee,
                due_date=anchor_date + timedelta(days=task.due_offset_days),
                sort_order=task.sort_order,
            )
        )
    record_by(
        db,
        actor,
        f"hr.{kind}.start",
        entity_type="employee",
        entity_id=str(employee_id),
        changes={"checklist": {"from": None, "to": template.name}},
    )
    return checklist.id


def start_onboarding(db: Session, actor: Actor, employee_id: uuid.UUID) -> uuid.UUID:
    authz.require(db, actor, "hr.checklist.manage")
    row = people.get_person(db, actor, employee_id)
    if row.status == "terminated":
        raise Conflict("This person has left the organisation.")
    return start(db, actor, row.id, kind="onboarding", anchor_date=row.hire_date or clock.today())


def cancel_for_exit(db: Session, actor: Actor | None, exit_id: uuid.UUID) -> None:
    for checklist in db.scalars(
        select(Checklist).where(Checklist.exit_id == exit_id, Checklist.completed_at.is_(None))
    ):
        checklist.cancelled_at = clock.now()
        record_by(
            db,
            actor,
            "hr.offboarding.cancel",
            entity_type="employee",
            entity_id=str(checklist.employee_id),
        )


def _may_complete(
    actor_employee: uuid.UUID | None,
    powers: tuple[bool, bool],
    checklist: Checklist,
    assignee: str,
) -> bool:
    manage, it = powers
    if manage:
        return True
    if assignee == "it":
        return it
    if assignee == "manager":
        return actor_employee is not None and actor_employee == checklist.manager_employee_id
    if assignee == "employee":
        return actor_employee is not None and actor_employee == checklist.employee_id
    return False


def _powers(db: Session, actor: Actor) -> tuple[bool, bool]:
    return (
        bool(engine.check(db, actor.user_id, "hr.checklist.manage")),
        bool(engine.check(db, actor.user_id, "hr.checklist.it")),
    )


def _views(
    db: Session, actor: Actor, checklists: list[Checklist], *, keep_all: bool
) -> list[ChecklistView]:
    if not checklists:
        return []
    actor_employee = people.employee_of_user(db, actor.user_id)
    powers = _powers(db, actor)
    today = clock.today()
    ids = [c.id for c in checklists]
    tasks: dict[uuid.UUID, list[ChecklistTask]] = {}
    for t in db.scalars(
        select(ChecklistTask)
        .where(ChecklistTask.checklist_id.in_(ids))
        .order_by(ChecklistTask.sort_order)
    ):
        tasks.setdefault(t.checklist_id, []).append(t)
    employees = {
        e.id: e
        for e in db.scalars(
            select(Employee).where(Employee.id.in_({c.employee_id for c in checklists}))
        )
    }
    held = org.positions_held(db, list(employees))
    doers = {
        u.id: u.full_name
        for u in db.scalars(
            select(User).where(
                User.id.in_({t.done_by for ts in tasks.values() for t in ts if t.done_by})
            )
        )
    }
    exits = {
        e.id: e
        for e in db.scalars(
            select(EmployeeExit).where(
                EmployeeExit.id.in_({c.exit_id for c in checklists if c.exit_id})
            )
        )
    }
    result = []
    for c in checklists:
        views = [
            TaskView(
                id=t.id,
                title=t.title,
                assignee=t.assignee,
                due_date=t.due_date,
                done_at=t.done_at,
                done_by=doers.get(t.done_by) if t.done_by else None,
                can_complete=_may_complete(actor_employee, powers, c, t.assignee),
                overdue=t.done_at is None and t.due_date < today,
            )
            for t in tasks.get(c.id, [])
        ]
        if not keep_all and not any(v.can_complete for v in views):
            continue
        employee = employees[c.employee_id]
        positions = held.get(employee.id, [])
        exit_row = exits.get(c.exit_id) if c.exit_id else None
        result.append(
            ChecklistView(
                id=c.id,
                kind=c.kind,
                name=c.name,
                employee_id=employee.id,
                employee_name=employee.full_name,
                position=positions[0][0] if positions else None,
                anchor_date=c.anchor_date,
                access_ends=access_end(exit_row.last_working_day) if exit_row else None,
                completed_at=c.completed_at,
                tasks=views,
            )
        )
    return result


def list_open(
    db: Session, actor: Actor, *, kind: str = "", mine: bool = False, overdue: bool = False
) -> list[ChecklistView]:
    """Open checklists with a task the actor can complete (every one for HR). `mine`
    keeps only checklists with an open task the actor can do."""
    stmt = select(Checklist).where(
        Checklist.completed_at.is_(None), Checklist.cancelled_at.is_(None)
    )
    if kind in CHECKLIST_KINDS:
        stmt = stmt.where(Checklist.kind == kind)
    rows = list(db.scalars(stmt.order_by(Checklist.anchor_date).limit(300)))
    views = _views(db, actor, rows, keep_all=False)
    if mine:
        views = [v for v in views if any(t.can_complete and not t.done_at for t in v.tasks)]
    if overdue:
        views = [v for v in views if v.overdue]
    return views


def for_employee(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[ChecklistView]:
    """Checklists on an employee's profile (the profile itself is already authorised)."""
    rows = list(
        db.scalars(
            select(Checklist)
            .where(Checklist.employee_id == employee_id, Checklist.cancelled_at.is_(None))
            .order_by(Checklist.created_at.desc())
            .limit(5)
        )
    )
    keep_all = bool(engine.check(db, actor.user_id, "hr.checklist.manage"))
    return _views(db, actor, rows, keep_all=keep_all)


def get(db: Session, actor: Actor, checklist_id: uuid.UUID) -> ChecklistView:
    row = db.get(Checklist, checklist_id)
    if row is None or row.cancelled_at is not None:
        raise NotFound()
    views = _views(db, actor, [row], keep_all=False)
    if not views:
        raise NotFound()
    return views[0]


def my_open_tasks(db: Session, actor: Actor) -> list[tuple[ChecklistView, TaskView]]:
    return [
        (c, t)
        for c in list_open(db, actor, mine=True)
        for t in c.tasks
        if t.can_complete and not t.done_at
    ]


def set_done(db: Session, actor: Actor, task_id: uuid.UUID, *, done: bool) -> uuid.UUID:
    task = db.get(ChecklistTask, task_id)
    if task is None:
        raise NotFound()
    checklist = db.get(Checklist, task.checklist_id)
    if checklist is None or checklist.cancelled_at is not None:
        raise NotFound()
    if not _may_complete(
        people.employee_of_user(db, actor.user_id), _powers(db, actor), checklist, task.assignee
    ):
        raise PermissionDenied()
    if (task.done_at is not None) == done:
        return checklist.id
    task.done_at, task.done_by = (clock.now(), actor.user_id) if done else (None, None)
    db.flush()
    remaining = db.scalar(
        select(ChecklistTask.id).where(
            ChecklistTask.checklist_id == checklist.id, ChecklistTask.done_at.is_(None)
        )
    )
    checklist.completed_at = None if remaining else clock.now()
    record_by(
        db,
        actor,
        "hr.checklist_task.done" if done else "hr.checklist_task.reopen",
        entity_type="employee",
        entity_id=str(checklist.employee_id),
        changes={"task": {"from": None, "to": task.title}},
    )
    return checklist.id
