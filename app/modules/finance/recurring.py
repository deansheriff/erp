"""Recurring journals (ADR 0023): templates drafted on their day each month, from a first
month to an optional last one. The template's author drafted each one, so someone else
posts it."""

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import clock
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import journals
from app.modules.finance.models import (
    Account,
    Journal,
    JournalLine,
    RecurringJournal,
    RecurringLine,
)
from app.org import service as org

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class TemplateView:
    id: uuid.UUID
    name: str
    memo: str
    day_of_month: int
    first_month: date
    last_month: date | None
    next_date: date | None
    is_active: bool
    total: Decimal
    lines: list[journals.LineView] = field(default_factory=list)


def _month_day(month: date, day: int) -> date:
    return date(month.year, month.month, min(day, calendar.monthrange(month.year, month.month)[1]))


def _next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _check(
    *,
    name: str,
    memo: str,
    day_of_month: str,
    first_month: date | None,
    last_month: date | None,
    parsed: list[journals.Parsed],
    errors: dict[str, str],
) -> int:
    """Add the template's problems to `errors`; return the day of the month."""
    if not name.strip():
        errors["name"] = "Name the recurring journal."
    if not memo.strip():
        errors["memo"] = "Say what each journal is for."
    try:
        day = int(day_of_month)
    except ValueError:
        day = 0
    if not 1 <= day <= 28:
        errors["day_of_month"] = "Choose a day from 1 to 28."
    if first_month is None:
        errors["first_month"] = "Choose the first month."
    elif last_month is not None and last_month < first_month:
        errors["last_month"] = "The last month can't be before the first."
    debit = sum((line[3] for line in parsed), ZERO)
    credit = sum((line[4] for line in parsed), ZERO)
    if not errors and (len(parsed) < 2 or debit != credit):
        errors["form"] = "The lines must balance, with at least two of them."
    return day


def save_template(
    db: Session,
    actor: Actor,
    template_id: uuid.UUID | None,
    *,
    name: str,
    memo: str,
    day_of_month: str,
    first_month: date | None,
    last_month: date | None,
    is_active: bool,
    lines: list[journals.LineInput],
) -> uuid.UUID:
    if not engine.check(db, actor.user_id, journals.CREATE):
        raise PermissionDenied()
    parsed, errors = journals.parse_lines(db, lines)
    day = _check(
        name=name,
        memo=memo,
        day_of_month=day_of_month,
        first_month=first_month,
        last_month=last_month,
        parsed=parsed,
        errors=errors,
    )
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert first_month is not None  # noqa: S101  (checked above)
    first = first_month.replace(day=1)
    last = last_month.replace(day=1) if last_month else None
    if template_id:
        row = db.get(RecurringJournal, template_id)
        if row is None:
            raise NotFound()
    else:
        row = RecurringJournal(created_by=actor.user_id)
        db.add(row)
    row.name, row.memo = name.strip()[:120], memo.strip()[:200]
    row.day_of_month, row.first_month, row.last_month, row.is_active = day, first, last, is_active
    # The next draft: this month's (drafted at once if its day has passed) or, if later,
    # the first month's. Earlier months are not caught up.
    upcoming = _month_day(max(first, clock.today().replace(day=1)), day)
    row.next_date = None if last and upcoming > _month_day(last, day) else upcoming
    row.created_by = actor.user_id  # the drafts carry the author: someone else posts them
    db.flush()
    for old in db.scalars(select(RecurringLine).where(RecurringLine.recurring_id == row.id)):
        db.delete(old)
    db.flush()
    # Recurring templates don't name projects (ADR 0031).
    for position, (account, centre, description, d, c, _) in enumerate(parsed, start=1):
        db.add(
            RecurringLine(
                recurring_id=row.id,
                position=position,
                account_id=account.id,
                cost_centre_id=centre,
                description=description,
                debit=d,
                credit=c,
            )
        )
    db.flush()
    record_by(
        db, actor, "finance.recurring.save", entity_type="recurring_journal", entity_id=str(row.id)
    )
    return row.id


def generate_due(db: Session, today: date | None = None) -> int:
    """Draft every active template whose next date has come (catching up missed months).
    Returns how many drafts were made."""
    today = today or clock.today()
    made = 0
    due = list(
        db.scalars(
            select(RecurringJournal)
            .where(
                RecurringJournal.is_active,
                RecurringJournal.next_date.is_not(None),
                RecurringJournal.next_date <= today,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for template in due:
        lines = list(
            db.scalars(
                select(RecurringLine)
                .where(RecurringLine.recurring_id == template.id)
                .order_by(RecurringLine.position)
            )
        )
        codes = dict(
            db.execute(
                select(Account.id, Account.code).where(
                    Account.id.in_([line.account_id for line in lines])
                )
            ).all()
        )
        while template.next_date is not None and template.next_date <= today:
            journal = Journal(
                journal_date=template.next_date,
                memo=template.memo,
                source="recurring",
                status="draft",
                created_by=template.created_by,
                recurring_id=template.id,
            )
            db.add(journal)
            db.flush()
            for line in lines:
                db.add(
                    JournalLine(
                        journal_id=journal.id,
                        position=line.position,
                        account_code=codes[line.account_id],
                        account_id=line.account_id,
                        cost_centre_id=line.cost_centre_id,
                        description=line.description,
                        debit=line.debit,
                        credit=line.credit,
                    )
                )
            made += 1
            following = _month_day(
                _next_month(template.next_date.replace(day=1)), template.day_of_month
            )
            ended = template.last_month and following > _month_day(
                template.last_month, template.day_of_month
            )
            template.next_date = None if ended else following
            db.flush()
    return made


def list_templates(db: Session, actor: Actor) -> list[TemplateView]:
    if not engine.check(db, actor.user_id, journals.READ):
        raise PermissionDenied()
    templates = list(db.scalars(select(RecurringJournal).order_by(RecurringJournal.name)))
    lines = (
        list(
            db.scalars(
                select(RecurringLine)
                .where(RecurringLine.recurring_id.in_([t.id for t in templates]))
                .order_by(RecurringLine.position)
            )
        )
        if templates
        else []
    )
    names = {a.id: a for a in db.scalars(select(Account))}
    centres = dict(org.cost_centre_choices(db))
    by_template: dict[uuid.UUID, list[journals.LineView]] = {}
    for line in lines:
        account = names.get(line.account_id)
        by_template.setdefault(line.recurring_id, []).append(
            journals.LineView(
                account_code=account.code if account else "",
                account_name=account.name if account else None,
                cost_centre=centres.get(line.cost_centre_id) if line.cost_centre_id else None,
                description=line.description,
                debit=line.debit,
                credit=line.credit,
                account_id=line.account_id,
                cost_centre_id=line.cost_centre_id,
            )
        )
    return [
        TemplateView(
            id=t.id,
            name=t.name,
            memo=t.memo,
            day_of_month=t.day_of_month,
            first_month=t.first_month,
            last_month=t.last_month,
            next_date=t.next_date,
            is_active=t.is_active,
            total=sum((line.debit for line in by_template.get(t.id, [])), ZERO),
            lines=by_template.get(t.id, []),
        )
        for t in templates
    ]
