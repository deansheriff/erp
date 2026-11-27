"""Shared helpers for projects (ADR 0031): permission keys, parsing, who may see or
manage a project, and the settings row."""

import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.core.errors import NotFound, PermissionDenied
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.projects.models import Member, Project, ProjectSettings

CREATE = "projects.project.create"
READ = "projects.project.read"  # every project, with figures
LOG = "projects.time.log"
APPROVE = "projects.time.approve"
APPROVE_ANY = "projects.time.approve_any"
RATES = "projects.rate.manage"
SETTINGS = "projects.settings.manage"
ZERO = Decimal(0)
CENT = Decimal("0.01")
STATUS_LABELS = {
    "planned": "Planned",
    "active": "Active",
    "on_hold": "On hold",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
STATUS_TONES = {
    "planned": "neutral",
    "active": "success",
    "on_hold": "warning",
    "completed": "info",
    "cancelled": "neutral",
}
CATEGORY_LABELS = {
    "labour": "Labour",
    "materials": "Materials",
    "expenses": "Expenses",
    "other": "Other",
}


def can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor, permission):
        raise PermissionDenied()


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def parse_money(text: str, key: str, errors: dict[str, str]) -> Decimal:
    raw = (text or "").strip().replace(",", "").replace("₦", "")
    if not raw:
        return ZERO
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal(-1)
    if not value.is_finite() or value < 0 or value != value.quantize(CENT):
        errors[key] = "Enter an amount, like 250,000.00."
        return ZERO
    return value


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def me(db: Session, actor: Actor) -> uuid.UUID | None:
    return people.employee_of_user(db, actor.user_id)


def project_of(db: Session, project_id: uuid.UUID, *, lock: bool = False) -> Project:
    query = select(Project).where(Project.id == project_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def member_ids(db: Session, project_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.scalars(select(Member.employee_id).where(Member.project_id == project_id)))


def on_team(db: Session, row: Project, employee_id: uuid.UUID | None) -> bool:
    """The manager and the members."""
    return employee_id is not None and (
        employee_id == row.manager_employee_id or employee_id in member_ids(db, row.id)
    )


def can_see(db: Session, actor: Actor, row: Project) -> bool:
    return on_team(db, row, me(db, actor)) or can(db, actor, READ)


def can_manage(db: Session, actor: Actor, row: Project) -> bool:
    """The project's manager (with the Project Manager permission), or someone who may
    both create projects and see them all."""
    if not can(db, actor, CREATE):
        return False
    return me(db, actor) == row.manager_employee_id or can(db, actor, READ)


def can_see_figures(db: Session, actor: Actor, row: Project) -> bool:
    return me(db, actor) == row.manager_employee_id or can(db, actor, READ)


def settings(db: Session) -> ProjectSettings:
    row = db.get(ProjectSettings, 1)
    if row is None:  # seeded by the migration; recreated if it ever goes missing
        row = ProjectSettings(id=1, warn_at=80, alert_at=100)
        db.add(row)
        db.flush()
    return row
