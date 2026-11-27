"""Support setup (ADR 0032): teams with a lead and agents, categories routed to a team,
SLA targets by priority, business hours, the warning level and when resolved tickets
close. All seeded values are to confirm."""

import uuid
from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.leave import service as leave
from app.modules.support import sla
from app.modules.support.models import (
    PRIORITIES,
    SlaTarget,
    SupportSettings,
    SupportTeam,
    TeamAgent,
    TicketCategory,
)

CREATE = "support.ticket.create"
READ = "support.ticket.read"
WORK = "support.ticket.work"
REPORT = "support.report.read"
MANAGE = "support.settings.manage"
PRIORITY_LABELS = {"low": "Low", "normal": "Normal", "high": "High", "urgent": "Urgent"}
PRIORITY_TONES = {"low": "neutral", "normal": "info", "high": "warning", "urgent": "danger"}
STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In progress",
    "waiting": "Waiting on requester",
    "resolved": "Resolved",
    "closed": "Closed",
}
STATUS_TONES = {
    "open": "info",
    "in_progress": "warning",
    "waiting": "neutral",
    "resolved": "success",
    "closed": "neutral",
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


def user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def settings(db: Session) -> SupportSettings:
    row = db.get(SupportSettings, 1)
    if row is None:  # seeded by the migration; recreated if it ever goes missing
        row = SupportSettings(
            id=1, day_start=time(8), day_end=time(17), warn_pct=75, close_after_days=3
        )
        db.add(row)
        db.flush()
    return row


def targets(db: Session) -> dict[str, SlaTarget]:
    return {t.priority: t for t in db.scalars(select(SlaTarget))}


def day_minutes(db: Session) -> int:
    row = settings(db)
    start, end = row.day_start, row.day_end
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def holidays_for(db: Session) -> sla.Holidays:
    def found(start: date, end: date) -> set[date]:
        return set(leave.holidays(db, start, end))

    return found


# ------------------------------------------------------------------ teams


def teams(db: Session, *, active_only: bool = False) -> list[SupportTeam]:
    query = select(SupportTeam).order_by(SupportTeam.name)
    if active_only:
        query = query.where(SupportTeam.is_active)
    return list(db.scalars(query))


def agents_of(db: Session, team_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.scalars(select(TeamAgent.employee_id).where(TeamAgent.team_id == team_id)))


def teams_of(db: Session, employee_id: uuid.UUID | None) -> set[uuid.UUID]:
    """Teams the person is an agent or the lead of."""
    if employee_id is None:
        return set()
    agent = set(db.scalars(select(TeamAgent.team_id).where(TeamAgent.employee_id == employee_id)))
    lead = set(
        db.scalars(select(SupportTeam.id).where(SupportTeam.lead_employee_id == employee_id))
    )
    return agent | lead


@dataclass(frozen=True, slots=True)
class TeamInput:
    name: str
    lead_id: str
    agents: list[str]
    active: bool


def save_team(db: Session, actor: Actor, team_id: uuid.UUID | None, data: TeamInput) -> uuid.UUID:
    require(db, actor, MANAGE)
    row = db.get(SupportTeam, team_id) if team_id else None
    if team_id and row is None:
        raise NotFound()
    errors: dict[str, str] = {}
    staff = set(people.current_ids(db))
    name = data.name.strip()[:80]
    if not name:
        errors["team_name"] = "Name the team."
    elif db.scalar(
        select(SupportTeam.id).where(SupportTeam.name == name, SupportTeam.id != team_id)
    ):
        errors["team_name"] = "There's already a team with that name."
    lead = uuid_or_none(data.lead_id)
    if data.lead_id and lead not in staff:
        errors["lead_id"] = "Choose someone on the staff list."
    agents = {a for a in (uuid_or_none(x) for x in data.agents) if a}
    if agents - staff:
        errors["agents"] = "Choose people on the staff list."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if row is None:
        row = SupportTeam(name=name)
        db.add(row)
        db.flush()
    row.name, row.lead_employee_id, row.is_active = name, lead, data.active
    current = {
        a.employee_id: a for a in db.scalars(select(TeamAgent).where(TeamAgent.team_id == row.id))
    }
    for employee_id, agent in current.items():
        if employee_id not in agents:
            db.delete(agent)
    for employee_id in agents - set(current):
        db.add(TeamAgent(team_id=row.id, employee_id=employee_id))
    db.flush()
    record_by(
        db,
        actor,
        "support.team.save",
        entity_type="support_team",
        entity_id=str(row.id),
        changes={"agents": len(agents)},
    )
    return row.id


def categories(db: Session, *, active_only: bool = False) -> list[TicketCategory]:
    query = select(TicketCategory).order_by(TicketCategory.name)
    if active_only:
        query = query.join(SupportTeam, SupportTeam.id == TicketCategory.team_id).where(
            TicketCategory.is_active, SupportTeam.is_active
        )
    return list(db.scalars(query))


def save_category(
    db: Session,
    actor: Actor,
    category_id: uuid.UUID | None,
    *,
    name: str,
    team_id: str,
    active: bool,
) -> None:
    require(db, actor, MANAGE)
    row = db.get(TicketCategory, category_id) if category_id else None
    if category_id and row is None:
        raise NotFound()
    errors: dict[str, str] = {}
    team = db.get(SupportTeam, uuid_or_none(team_id) or uuid.uuid4())
    if team is None:
        errors["team_id"] = "Choose the team that handles it."
    if not name.strip():
        errors["category_name"] = "Name the category."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert team is not None  # noqa: S101
    if row is None:
        row = TicketCategory(team_id=team.id, name=name.strip()[:80])
        db.add(row)
    row.team_id, row.name, row.is_active = team.id, name.strip()[:80], active
    try:
        db.flush()
    except Exception as exc:  # the (team, name) unique constraint
        raise Conflict("That team already has a category with that name.") from exc
    record_by(
        db, actor, "support.category.save", entity_type="support_category", entity_id=str(row.id)
    )


# ------------------------------------------------------------------ targets and hours


@dataclass(frozen=True, slots=True)
class SettingsInput:
    day_start: str
    day_end: str
    warn_pct: str
    close_after_days: str
    response_hours: dict[str, str]  # by priority
    resolve_hours: dict[str, str]


def _time(text: str, key: str, errors: dict[str, str]) -> time:
    try:
        hour, minute = (int(x) for x in text.strip().split(":"))
        return time(hour, minute)
    except ValueError:
        errors[key] = "Enter a time, like 08:00."
        return time(0)


def _hours(text: str, key: str, errors: dict[str, str]) -> int:
    try:
        minutes = round(float(text.strip()) * 60)
    except ValueError:
        minutes = 0
    if minutes <= 0:
        errors[key] = "Enter hours, like 4 or 1.5."
    return minutes


def _number(text: str, key: str, low: int, high: int, errors: dict[str, str]) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        value = 0
    if not low <= value <= high:
        errors[key] = f"Enter a number from {low} to {high}."
    return value


def save_settings(db: Session, actor: Actor, data: SettingsInput) -> None:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    start = _time(data.day_start, "day_start", errors)
    end = _time(data.day_end, "day_end", errors)
    if not errors and end <= start:
        errors["day_end"] = "The day must end after it starts."
    warn = _number(data.warn_pct, "warn_pct", 1, 99, errors)
    close = _number(data.close_after_days, "close_after_days", 1, 30, errors)
    minutes = {}
    for priority in PRIORITIES:
        response = _hours(data.response_hours.get(priority, ""), f"response_{priority}", errors)
        resolve = _hours(data.resolve_hours.get(priority, ""), f"resolve_{priority}", errors)
        if response and resolve and resolve < response:
            errors[f"resolve_{priority}"] = "Resolution can't be quicker than first response."
        minutes[priority] = (response, resolve)
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = settings(db)
    row.day_start, row.day_end, row.warn_pct, row.close_after_days = start, end, warn, close
    found = targets(db)
    for priority, (response, resolve) in minutes.items():
        target = found.get(priority)
        if target is None:
            target = SlaTarget(
                priority=priority, response_minutes=response, resolve_minutes=resolve
            )
            db.add(target)
        target.response_minutes, target.resolve_minutes = response, resolve
    db.flush()
    record_by(
        db,
        actor,
        "support.settings.update",
        entity_type="support_settings",
        entity_id="1",
        changes={"targets": {p: list(m) for p, m in minutes.items()}},
    )
