"""How finance (and the modules posting through it) names projects without importing the
projects module (ADR 0031). Projects implements `ProjectLink`; `app/wiring.py` connects it
with `set_link` at start-up."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.modules.finance.models import Account, Customer, Journal, JournalLine


class ProjectLink(Protocol):
    def choices(self, db: Session) -> list[tuple[uuid.UUID, str]]:
        """Active projects as (id, "code · name"): the ones costs can be recorded against."""
        ...

    def is_open(self, db: Session, project_id: uuid.UUID) -> bool: ...

    def labels(self, db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]: ...


_link: ProjectLink | None = None


def set_link(link: ProjectLink) -> None:
    global _link  # noqa: PLW0603  (set once at start-up by app/wiring.py)
    _link = link


def link() -> ProjectLink:
    if _link is None:
        raise RuntimeError("Projects aren't connected: call app.wiring.configure_runtime().")
    return _link


def check(db: Session, value: str, key: str, errors: dict[str, str]) -> uuid.UUID | None:
    """A form's project: blank, or an active project (else an error under `key`)."""
    if not value:
        return None
    try:
        project_id = uuid.UUID(value)
    except ValueError:
        project_id = None
    if project_id is None or not link().is_open(db, project_id):
        errors[key] = "Choose an active project, or none."
        return None
    return project_id


# ------------------------------------------------------------------ the ledger by project


@dataclass(frozen=True, slots=True)
class ProjectTotal:
    """Posted amounts naming a project, by account type and journal source (a reversal
    counts under its original's source). `net` is debit less credit."""

    project_id: uuid.UUID
    account_type: str
    source: str
    net: Decimal


def project_totals(
    db: Session,
    project_ids: set[uuid.UUID],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[ProjectTotal]:
    if not project_ids:
        return []
    original = aliased(Journal)
    source = func.coalesce(original.source, Journal.source)
    query = (
        select(
            JournalLine.project_id,
            Account.type,
            source,
            func.sum(JournalLine.debit - JournalLine.credit),
        )
        .join(Journal, Journal.id == JournalLine.journal_id)
        .join(Account, Account.id == JournalLine.account_id)
        .outerjoin(original, original.id == Journal.reverses_id)
        .where(JournalLine.project_id.in_(project_ids), Journal.status == "posted")
        .group_by(JournalLine.project_id, Account.type, source)
    )
    if start:
        query = query.where(Journal.journal_date >= start)
    if end:
        query = query.where(Journal.journal_date <= end)
    return [ProjectTotal(p, t, s, n) for p, t, s, n in db.execute(query).all() if p]


@dataclass(frozen=True, slots=True)
class ProjectPosting:
    journal_id: uuid.UUID
    number: str
    journal_date: date
    memo: str
    account: str
    account_type: str
    net: Decimal  # debit less credit


def project_postings(
    db: Session, project_id: uuid.UUID, *, limit: int = 20
) -> list[ProjectPosting]:
    """The project's latest posted ledger lines, newest first."""
    rows = db.execute(
        select(Journal, JournalLine, Account)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.project_id == project_id, Journal.status == "posted")
        .order_by(Journal.journal_date.desc(), Journal.number.desc())
        .limit(limit)
    ).all()
    return [
        ProjectPosting(
            j.id,
            j.number or "",
            j.journal_date,
            j.memo,
            f"{a.code} {a.name}",
            a.type,
            line.debit - line.credit,
        )
        for j, line, a in rows
    ]


def customer_names(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Customers' names, for a project's client."""
    if not ids:
        return {}
    return dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(ids))).all())


def customer_email(db: Session, customer_id: uuid.UUID) -> str | None:
    """A customer's email address, for support updates (ADR 0032)."""
    return db.scalar(select(Customer.email).where(Customer.id == customer_id))
