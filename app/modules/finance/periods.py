"""Financial years and their monthly periods (ADR 0023): January to December. A Finance
Manager opens a year, closes months and reopens one with a reason. Once all twelve are
closed, closing the year posts its income and expenses to retained earnings, for good."""

import calendar
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.finance.models import FinanceSettings, FiscalPeriod, FiscalYear, Journal

MANAGE = "finance.period.manage"
MIN_REASON = 5
MONTHS = tuple(calendar.month_name)[1:]


@dataclass(frozen=True, slots=True)
class PeriodView:
    id: uuid.UUID
    number: int
    name: str
    start_date: date
    end_date: date
    status: str
    posted: int
    drafts: int
    closed_by: str | None
    closed_at: object
    reopen_reason: str | None


@dataclass(frozen=True, slots=True)
class YearView:
    id: uuid.UUID
    year: int
    status: str
    periods: list[PeriodView]

    @property
    def all_closed(self) -> bool:
        return all(p.status == "closed" for p in self.periods)


def _require(db: Session, actor: Actor) -> None:
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()


def settings(db: Session) -> FinanceSettings:
    row = db.get(FinanceSettings, 1)
    if row is None:  # seeded by migration 0012
        raise Conflict("Finance settings are missing.")
    return row


def period_for(db: Session, day: date) -> tuple[FiscalPeriod | None, str | None]:
    """The open month a journal dated `day` can post into, or why it can't."""
    row = db.execute(
        select(FiscalPeriod, FiscalYear)
        .join(FiscalYear, FiscalYear.id == FiscalPeriod.year_id)
        .where(FiscalPeriod.start_date <= day, FiscalPeriod.end_date >= day)
    ).first()
    label = f"{MONTHS[day.month - 1]} {day.year}"
    if row is None:
        return None, f"The financial year {day.year} isn't open."
    period, year = row
    if year.status == "closed":
        return None, f"The financial year {day.year} is closed."
    if period.status == "closed":
        return None, f"{label} is closed."
    return period, None


def open_year(db: Session, actor: Actor, year: int) -> uuid.UUID:
    _require(db, actor)
    if not 2000 <= year <= 2100:
        raise ValidationFailed(field_errors={"year": "Enter a year between 2000 and 2100."})
    if db.scalars(select(FiscalYear).where(FiscalYear.year == year)).first():
        raise Conflict(f"The financial year {year} is already open.")
    row = FiscalYear(
        year=year,
        start_date=date(year, 1, 1),
        end_date=date(year, 12, 31),
        opened_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for month in range(1, 13):
        db.add(
            FiscalPeriod(
                year_id=row.id,
                number=month,
                start_date=date(year, month, 1),
                end_date=date(year, month, calendar.monthrange(year, month)[1]),
            )
        )
    db.flush()
    record_by(db, actor, "finance.year.open", entity_type="fiscal_year", entity_id=str(row.id))
    return row.id


def _period(db: Session, period_id: uuid.UUID) -> tuple[FiscalPeriod, FiscalYear]:
    period = db.scalars(
        select(FiscalPeriod).where(FiscalPeriod.id == period_id).with_for_update()
    ).first()
    if period is None:
        raise NotFound()
    year = db.get(FiscalYear, period.year_id)
    assert year is not None  # noqa: S101  (foreign key)
    return period, year


def close_period(db: Session, actor: Actor, period_id: uuid.UUID) -> None:
    _require(db, actor)
    period, _ = _period(db, period_id)
    if period.status == "closed":
        raise Conflict("This month is already closed.")
    drafts = db.scalar(
        select(func.count())
        .select_from(Journal)
        .where(
            Journal.status == "draft",
            Journal.journal_date >= period.start_date,
            Journal.journal_date <= period.end_date,
        )
    )
    if drafts:
        name = MONTHS[period.number - 1]
        raise Conflict(
            f"{drafts} draft journal{'s are' if drafts != 1 else ' is'} dated in {name}; "
            "post or delete them first."
        )
    period.status, period.closed_by, period.closed_at = "closed", actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "finance.period.close",
        entity_type="fiscal_period",
        entity_id=str(period.id),
        changes={"status": {"from": "open", "to": "closed"}},
    )


def reopen_period(db: Session, actor: Actor, period_id: uuid.UUID, *, reason: str) -> None:
    _require(db, actor)
    period, year = _period(db, period_id)
    if year.status == "closed":
        raise Conflict(f"The financial year {year.year} is closed and can't be reopened.")
    if period.status == "open":
        raise Conflict("This month is already open.")
    reason = reason.strip()
    if len(reason) < MIN_REASON:
        raise ValidationFailed(field_errors={"reason": "Say why it needs reopening."})
    period.status = "open"
    period.reopened_by, period.reopened_at, period.reopen_reason = (
        actor.user_id,
        clock.now(),
        reason,
    )
    db.flush()
    record_by(
        db,
        actor,
        "finance.period.reopen",
        entity_type="fiscal_period",
        entity_id=str(period.id),
        reason=reason,
        changes={"status": {"from": "closed", "to": "open"}},
    )


def save_settings(db: Session, actor: Actor, *, retained_earnings_code: str) -> None:
    from app.modules.finance import accounts  # noqa: PLC0415  (avoids an import cycle)

    _require(db, actor)
    code = retained_earnings_code.strip()
    account = accounts.by_codes(db, {code}).get(code)
    if account is None or account.type != "equity" or account.is_header:
        raise ValidationFailed(
            field_errors={"retained_earnings_code": "Choose an equity account that takes postings."}
        )
    row = settings(db)
    before = row.retained_earnings_code
    row.retained_earnings_code = code
    db.flush()
    record_by(
        db,
        actor,
        "finance.settings.update",
        entity_type="finance_settings",
        entity_id="1",
        changes={"retained_earnings_code": {"from": before, "to": code}},
    )


def years(db: Session, actor: Actor) -> list[YearView]:
    if not engine.check(db, actor.user_id, MANAGE) and not engine.check(
        db, actor.user_id, "finance.journal.read"
    ):
        raise PermissionDenied()
    counts: dict[tuple[uuid.UUID, str], int] = {}
    for period_id, status, n in db.execute(
        select(FiscalPeriod.id, Journal.status, func.count())
        .join(
            Journal,
            (Journal.journal_date >= FiscalPeriod.start_date)
            & (Journal.journal_date <= FiscalPeriod.end_date),
        )
        .group_by(FiscalPeriod.id, Journal.status)
    ).all():
        counts[(period_id, status)] = n
    who = dict(db.execute(select(User.id, User.full_name)).all())
    views = []
    for y in db.scalars(select(FiscalYear).order_by(FiscalYear.year.desc())):
        periods = list(
            db.scalars(
                select(FiscalPeriod)
                .where(FiscalPeriod.year_id == y.id)
                .order_by(FiscalPeriod.number)
            )
        )
        views.append(
            YearView(
                id=y.id,
                year=y.year,
                status=y.status,
                periods=[
                    PeriodView(
                        id=p.id,
                        number=p.number,
                        name=MONTHS[p.number - 1],
                        start_date=p.start_date,
                        end_date=p.end_date,
                        status=p.status,
                        posted=counts.get((p.id, "posted"), 0),
                        drafts=counts.get((p.id, "draft"), 0),
                        closed_by=who.get(p.closed_by) if p.closed_by else None,
                        closed_at=p.closed_at,
                        reopen_reason=p.reopen_reason,
                    )
                    for p in periods
                ],
            )
        )
    return views
