"""Reading the audit log for the viewer. The log is append-only; nothing here writes."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog

PAGE_SIZE = 50
OUTCOMES = ("success", "denied", "failure")


@dataclass(frozen=True, slots=True)
class AuditFilters:
    q: str = ""
    outcome: str = ""
    actor_id: uuid.UUID | None = None
    action_prefix: str = ""
    since: datetime | None = None
    until: datetime | None = None
    page: int = 1


@dataclass(frozen=True, slots=True)
class AuditPage:
    rows: list[AuditLog]
    total: int
    page: int
    pages: int

    @property
    def first(self) -> int:
        return 0 if not self.total else (self.page - 1) * PAGE_SIZE + 1

    @property
    def last(self) -> int:
        return min(self.page * PAGE_SIZE, self.total)


def _where(f: AuditFilters) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if f.q.strip():
        like = f"%{f.q.strip().lower()}%"
        clauses.append(
            or_(
                func.lower(AuditLog.action).like(like),
                func.lower(AuditLog.actor_label).like(like),
                func.lower(AuditLog.entity_id).like(like),
                func.lower(AuditLog.reason).like(like),
            )
        )
    if f.outcome in OUTCOMES:
        clauses.append(AuditLog.outcome == f.outcome)
    if f.actor_id is not None:
        clauses.append(AuditLog.actor_id == f.actor_id)
    if f.action_prefix:
        clauses.append(AuditLog.action.startswith(f.action_prefix))
    if f.since is not None:
        clauses.append(AuditLog.occurred_at >= f.since)
    if f.until is not None:
        clauses.append(AuditLog.occurred_at < f.until)
    return clauses


def search(db: Session, filters: AuditFilters) -> AuditPage:
    where = _where(filters)
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*where)) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, filters.page), pages)
    rows = list(
        db.scalars(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )
    return AuditPage(rows=rows, total=total, page=page, pages=pages)


def get(db: Session, event_id: uuid.UUID) -> AuditLog | None:
    return db.get(AuditLog, event_id)


def action_modules(db: Session) -> list[str]:
    """The first part of every recorded action (`identity`, `authz`, …), for the filter."""
    prefix = func.split_part(AuditLog.action, ".", 1)
    return [p for p in db.scalars(select(prefix).distinct().order_by(prefix)) if p]


def actors(db: Session) -> list[tuple[uuid.UUID, str]]:
    """People who appear in the log, for the filter."""
    rows = db.execute(
        select(AuditLog.actor_id, func.max(AuditLog.actor_label))
        .where(AuditLog.actor_id.is_not(None))
        .group_by(AuditLog.actor_id)
        .order_by(func.max(AuditLog.actor_label))
    ).all()
    return [(actor_id, label or "—") for actor_id, label in rows if actor_id is not None]
