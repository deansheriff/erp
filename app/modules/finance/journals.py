"""Journals (ADR 0023): manual ones drafted by one person and posted by another; system
journals from other modules posted at once or held with a reason; reversals; and the
year-end close. Numbers are gap-free and given at posting."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.finance import accounts, periods
from app.modules.finance.models import (
    Account,
    FiscalPeriod,
    FiscalYear,
    Journal,
    JournalLine,
)
from app.org import service as org

READ = "finance.journal.read"
CREATE = "finance.journal.create"
POST = "finance.journal.post"
SEQUENCE = "journal"
MIN_REASON = 5
CENT = Decimal("0.01")
ZERO = Decimal(0)
SOURCE_LABELS = {
    "manual": "Manual",
    "recurring": "Recurring",
    "reversal": "Reversal",
    "year_end": "Year end",
    "payroll": "Payroll",
    "expenses": "Expenses",
    "receivable": "Receivables",
    "payable": "Payables",
    "bank": "Banking",
    "tax": "Tax payment",
    "asset": "Fixed assets",
    "stock": "Inventory",
}
# Journals of finance's own documents, reversed from the document, not the Journals screen.
DOCUMENT_SOURCES = frozenset({"receivable", "payable", "bank", "tax", "asset", "stock"})
TABS = ("draft", "posted", "held")

# (account code, cost centre, debit, credit) as other modules hand them over
SystemLine = tuple[str, uuid.UUID | None, Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class LineInput:
    account_id: str
    cost_centre_id: str
    description: str
    debit: str
    credit: str


@dataclass(frozen=True, slots=True)
class LineView:
    account_code: str
    account_name: str | None
    cost_centre: str | None
    description: str | None
    debit: Decimal
    credit: Decimal
    account_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class JournalView:
    id: uuid.UUID
    number: str | None
    journal_date: date
    original_date: date | None
    memo: str
    source: str
    status: str
    hold_reason: str | None
    redate_reason: str | None
    created_by: str | None
    created_at: datetime
    posted_by: str | None
    posted_at: datetime | None
    total: Decimal
    reverses: tuple[uuid.UUID, str] | None = None
    reversed_by: tuple[uuid.UUID, str] | None = None
    lines: list[LineView] = field(default_factory=list)
    can_edit: bool = False
    can_post: bool = False
    post_blocked: str | None = None
    can_reverse: bool = False

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source]

    @property
    def debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), ZERO)

    @property
    def credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), ZERO)


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


# ------------------------------------------------------------------ drafts


def parse_amount(value: str, key: str, errors: dict[str, str]) -> Decimal:
    text = value.strip().replace(",", "").replace("₦", "")
    if not text:
        return ZERO
    try:
        amount = Decimal(text)
    except InvalidOperation:
        errors[key] = "Enter an amount, like 12,500.00."
        return ZERO
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        errors[key] = "Enter a positive amount in naira and kobo."
        return ZERO
    return amount


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def parse_lines(
    db: Session, lines: list[LineInput]
) -> tuple[list[tuple[Account, uuid.UUID | None, str | None, Decimal, Decimal]], dict[str, str]]:
    """Form lines to (account, cost centre, description, debit, credit), skipping empty
    rows. Errors are keyed `line_<n>` by the row's position on the form."""
    errors: dict[str, str] = {}
    known = {a.id: a for a in accounts.postable(db)}
    centres = {cid for cid, _ in org.cost_centre_choices(db)}
    parsed = []
    for i, line in enumerate(lines):
        if not (line.account_id or line.debit.strip() or line.credit.strip()):
            continue
        key = f"line_{i}"
        debit = parse_amount(line.debit, key, errors)
        credit = parse_amount(line.credit, key, errors)
        account = known.get(_uuid(line.account_id))  # type: ignore[arg-type]
        centre = _uuid(line.cost_centre_id)
        if key in errors:
            continue
        if account is None:
            errors[key] = "Choose an account that takes postings."
        elif (debit > 0) == (credit > 0):
            errors[key] = "Enter either a debit or a credit."
        elif centre is not None and centre not in centres:
            errors[key] = "Choose an active cost centre."
        else:
            parsed.append((account, centre, line.description.strip() or None, debit, credit))
    return parsed, errors


def _replace_lines(
    db: Session,
    journal: Journal,
    parsed: list[tuple[Account, uuid.UUID | None, str | None, Decimal, Decimal]],
) -> None:
    for old in db.scalars(select(JournalLine).where(JournalLine.journal_id == journal.id)):
        db.delete(old)
    db.flush()
    for position, (account, centre, description, debit, credit) in enumerate(parsed, start=1):
        db.add(
            JournalLine(
                journal_id=journal.id,
                position=position,
                account_code=account.code,
                account_id=account.id,
                cost_centre_id=centre,
                description=description,
                debit=debit,
                credit=credit,
            )
        )
    db.flush()


def save_draft(
    db: Session,
    actor: Actor,
    journal_id: uuid.UUID | None,
    *,
    journal_date: date | None,
    memo: str,
    lines: list[LineInput],
) -> uuid.UUID:
    """Create or change a draft. Drafts may be out of balance; posting checks."""
    if not _can(db, actor, CREATE):
        raise PermissionDenied()
    if journal_id and _locked(db, journal_id).status != "draft":
        raise Conflict("Only a draft can be changed.")
    parsed, errors = parse_lines(db, lines)
    if journal_date is None:
        errors["journal_date"] = "Enter the journal date."
    if not memo.strip():
        errors["memo"] = "Say what the journal is for."
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert journal_date is not None  # noqa: S101  (checked above)
    if journal_id:
        row = _locked(db, journal_id)
        if row.status != "draft":
            raise Conflict("Only a draft can be changed.")
    else:
        row = Journal(source="manual", status="draft", journal_date=journal_date, memo="")
        db.add(row)
    row.journal_date, row.memo = journal_date, memo.strip()[:200]
    row.created_by = actor.user_id  # whoever last drafted it can't post it
    db.flush()
    _replace_lines(db, row, parsed)
    record_by(
        db,
        actor,
        "finance.journal.draft",
        entity_type="journal",
        entity_id=str(row.id),
        changes={"lines": {"from": None, "to": len(parsed)}},
    )
    return row.id


def delete_draft(db: Session, actor: Actor, journal_id: uuid.UUID) -> None:
    if not _can(db, actor, CREATE):
        raise PermissionDenied()
    row = _locked(db, journal_id)
    if row.status != "draft":
        raise Conflict("Only a draft can be deleted.")
    record_by(db, actor, "finance.journal.delete", entity_type="journal", entity_id=str(row.id))
    db.delete(row)
    db.flush()


# ------------------------------------------------------------------ posting


def _locked(db: Session, journal_id: uuid.UUID) -> Journal:
    row = db.scalars(select(Journal).where(Journal.id == journal_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def _lines(db: Session, journal_id: uuid.UUID) -> list[JournalLine]:
    return list(
        db.scalars(
            select(JournalLine)
            .where(JournalLine.journal_id == journal_id)
            .order_by(JournalLine.position)
        )
    )


def _problem(db: Session, journal: Journal, lines: list[JournalLine]) -> str | None:
    """Why the journal can't post as it stands, or None. Changes nothing."""
    if len(lines) < 2:
        return "A journal needs at least two lines."
    debit = sum((line.debit for line in lines), ZERO)
    credit = sum((line.credit for line in lines), ZERO)
    if debit != credit or debit == 0:
        return f"Debits and credits differ by {abs(debit - credit):,.2f}."
    found = accounts.by_codes(db, {line.account_code for line in lines})
    for line in lines:
        account = found.get(line.account_code)
        if account is None:
            return f"Account {line.account_code} isn't in the chart of accounts."
        if account.is_header or not account.is_active:
            return f"Account {account.code} {account.name} doesn't take postings."
    _, reason = periods.period_for(db, journal.journal_date)
    return reason


def _post_now(db: Session, journal: Journal, poster: uuid.UUID | None) -> None:
    lines = _lines(db, journal.id)
    found = accounts.by_codes(db, {line.account_code for line in lines})
    for line in lines:
        line.account_id = found[line.account_code].id
    period, _ = periods.period_for(db, journal.journal_date)
    journal.number = numbering.next_number(db, SEQUENCE)
    journal.period_id = period.id if period else None
    journal.status, journal.hold_reason = "posted", None
    journal.posted_by, journal.posted_at = poster, clock.now()
    db.flush()


def _sod_blocked(db: Session, actor: Actor, journal: Journal) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), POST, actor.user_id, {"created_by": journal.created_by}, held
        )
    )


def post(db: Session, actor: Actor, journal_id: uuid.UUID) -> str:
    """Post a draft (someone else drafted it) or a held journal. Returns its number."""
    if not _can(db, actor, POST):
        raise PermissionDenied()
    journal = _locked(db, journal_id)
    if journal.status not in {"draft", "held"}:
        raise Conflict("This journal is already posted.")
    if _sod_blocked(db, actor, journal):
        raise PermissionDenied("You drafted this journal, so someone else posts it.")
    problem = _problem(db, journal, _lines(db, journal.id))
    if problem:
        raise Conflict(problem)
    before = journal.status
    _post_now(db, journal, actor.user_id)
    record_by(
        db,
        actor,
        "finance.journal.post",
        entity_type="journal",
        entity_id=str(journal.id),
        changes={
            "status": {"from": before, "to": "posted"},
            "number": {"from": None, "to": journal.number},
        },
    )
    assert journal.number is not None  # noqa: S101  (just numbered)
    return journal.number


def redate_held(
    db: Session, actor: Actor, journal_id: uuid.UUID, *, new_date: date | None, reason: str
) -> str:
    """Move a held journal into an open month, with a reason, and post it."""
    if not _can(db, actor, POST):
        raise PermissionDenied()
    journal = _locked(db, journal_id)
    if journal.status != "held":
        raise Conflict("Only a held journal can be re-dated.")
    reason = reason.strip()
    errors: dict[str, str] = {}
    if new_date is None:
        errors["new_date"] = "Choose a date in an open month."
    if len(reason) < MIN_REASON:
        errors["reason"] = "Say why it's re-dated."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert new_date is not None  # noqa: S101  (checked above)
    journal.original_date = journal.original_date or journal.journal_date
    journal.journal_date, journal.redate_reason = new_date, reason
    db.flush()
    return post(db, actor, journal.id)


def reverse(db: Session, actor: Actor, journal_id: uuid.UUID, *, on: date | None) -> uuid.UUID:
    """A new posted journal with the lines swapped. The original stays as it was."""
    if not _can(db, actor, POST):
        raise PermissionDenied()
    original = _locked(db, journal_id)
    if original.status != "posted":
        raise Conflict("Only a posted journal can be reversed.")
    if original.reversed_by_id:
        raise Conflict("This journal has already been reversed.")
    if original.source == "year_end":
        raise Conflict("The year-end closing journal can't be reversed.")
    if original.source in DOCUMENT_SOURCES:
        raise Conflict("Reverse this from its document: a receipt or payment, or raise a note.")
    if on is None:
        raise ValidationFailed(field_errors={"reverse_on": "Choose the reversal date."})
    reversal = _reverse_now(db, original, on, actor.user_id)
    record_by(
        db,
        actor,
        "finance.journal.reverse",
        entity_type="journal",
        entity_id=str(original.id),
        changes={"reversed_by": {"from": None, "to": reversal.number}},
    )
    return reversal.id


def _reverse_now(db: Session, original: Journal, on: date, actor_id: uuid.UUID | None) -> Journal:
    _, reason = periods.period_for(db, on)
    if reason:
        raise ValidationFailed(field_errors={"reverse_on": reason})
    reversal = Journal(
        journal_date=on,
        memo=f"Reversal of {original.number}: {original.memo}"[:200],
        source="reversal",
        status="draft",
        created_by=actor_id,
        reverses_id=original.id,
    )
    db.add(reversal)
    db.flush()
    for line in _lines(db, original.id):
        db.add(
            JournalLine(
                journal_id=reversal.id,
                position=line.position,
                account_code=line.account_code,
                account_id=line.account_id,
                cost_centre_id=line.cost_centre_id,
                description=line.description,
                debit=line.credit,
                credit=line.debit,
            )
        )
    db.flush()
    _post_now(db, reversal, actor_id)
    original.reversed_by_id = reversal.id
    db.flush()
    return reversal


def _build(
    db: Session,
    *,
    source: str,
    source_id: uuid.UUID,
    journal_date: date,
    memo: str,
    lines: list[SystemLine],
    created_by: uuid.UUID | None = None,
) -> Journal:
    """A held journal with the lines merged by account and cost centre."""
    journal = Journal(
        journal_date=journal_date,
        memo=memo[:200],
        source=source,
        source_id=source_id,
        status="held",
        created_by=created_by,
    )
    db.add(journal)
    db.flush()
    merged: dict[tuple[str, uuid.UUID | None], Decimal] = defaultdict(lambda: ZERO)
    for code, centre, debit, credit in lines:
        merged[(code, centre)] += debit - credit
    position = 0
    for (code, centre), net in merged.items():
        if net:
            position += 1
            db.add(
                JournalLine(
                    journal_id=journal.id,
                    position=position,
                    account_code=code,
                    cost_centre_id=centre,
                    debit=net if net > 0 else ZERO,
                    credit=-net if net < 0 else ZERO,
                )
            )
    db.flush()
    return journal


def post_document_journal(
    db: Session,
    *,
    source_id: uuid.UUID,
    journal_date: date,
    memo: str,
    lines: list[SystemLine],
    poster: uuid.UUID | None,
    source: str = "receivable",
) -> Journal:
    """Post the journal of a finance document (an invoice, credit note, receipt, bill,
    debit note or supplier payment) in the caller's transaction, or refuse with the
    reason: nothing is held (ADR 0024, 0025)."""
    journal = _build(
        db,
        source=source,
        source_id=source_id,
        journal_date=journal_date,
        memo=memo,
        lines=lines,
        created_by=poster,
    )
    problem = _problem(db, journal, _lines(db, journal.id))
    if problem:
        raise Conflict(problem)
    _post_now(db, journal, poster)
    return journal


def reverse_document_journal(
    db: Session, journal_id: uuid.UUID, *, on: date, actor_id: uuid.UUID | None
) -> Journal:
    """Reverse a finance document's journal on a date in an open month."""
    original = _locked(db, journal_id)
    if original.status != "posted" or original.reversed_by_id:
        raise Conflict("This journal can't be reversed.")
    return _reverse_now(db, original, on, actor_id)


def post_system_journal(
    db: Session,
    *,
    source: str,
    source_id: uuid.UUID,
    journal_date: date,
    memo: str,
    lines: list[SystemLine],
) -> tuple[uuid.UUID, bool]:
    """Take a balanced journal from another module, in its transaction. It posts at once,
    or is held with the reason. Returns (journal id, posted)."""
    existing = db.scalars(
        select(Journal).where(Journal.source == source, Journal.source_id == source_id)
    ).first()
    if existing is not None:
        return existing.id, existing.status == "posted"
    journal = _build(
        db, source=source, source_id=source_id, journal_date=journal_date, memo=memo, lines=lines
    )
    problem = _problem(db, journal, _lines(db, journal.id))
    if problem:
        journal.hold_reason = problem
        db.flush()
        return journal.id, False
    _post_now(db, journal, None)
    return journal.id, True


def statuses(
    db: Session, source: str, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str | None]]:
    """(status, number) of the journals other modules handed over, by their own id."""
    if not source_ids:
        return {}
    return {
        sid: (status, number)
        for sid, status, number in db.execute(
            select(Journal.source_id, Journal.status, Journal.number).where(
                Journal.source == source, Journal.source_id.in_(source_ids)
            )
        ).all()
        if sid
    }


# ------------------------------------------------------------------ the year end


def close_year(db: Session, actor: Actor, year_id: uuid.UUID) -> uuid.UUID | None:
    """Post the year's income and expenses to retained earnings and close the year. Every
    month must be closed first. Returns the closing journal (None if nothing to close)."""
    if not _can(db, actor, periods.MANAGE):
        raise PermissionDenied()
    year = db.scalars(select(FiscalYear).where(FiscalYear.id == year_id).with_for_update()).first()
    if year is None:
        raise NotFound()
    if year.status == "closed":
        raise Conflict(f"The financial year {year.year} is already closed.")
    open_months = db.scalar(
        select(func.count())
        .select_from(FiscalPeriod)
        .where(FiscalPeriod.year_id == year.id, FiscalPeriod.status == "open")
    )
    if open_months:
        raise Conflict("Close every month of the year first.")
    code = periods.settings(db).retained_earnings_code
    retained = accounts.by_codes(db, {code}).get(code)
    if retained is None or retained.is_header or not retained.is_active:
        raise Conflict(f"The retained earnings account {code} isn't in the chart; set it first.")
    rows = db.execute(
        select(Account, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            Journal.status == "posted",
            Journal.journal_date >= year.start_date,
            Journal.journal_date <= year.end_date,
            Account.type.in_(("income", "expense")),
        )
        .group_by(Account.id)
    ).all()
    journal_id = None
    lines = []
    net = ZERO
    for account, debit, credit in rows:
        balance = (debit or ZERO) - (credit or ZERO)  # debit-positive
        if balance:
            lines.append(
                (account, ZERO if balance > 0 else -balance, balance if balance > 0 else ZERO)
            )
            net += balance
    if lines:
        lines.append((retained, net if net > 0 else ZERO, -net if net < 0 else ZERO))
        journal = Journal(
            journal_date=year.end_date,
            memo=f"Close the financial year {year.year} to retained earnings",
            source="year_end",
            status="draft",
            created_by=actor.user_id,
        )
        db.add(journal)
        db.flush()
        position = 0
        for account, debit, credit in lines:
            if debit or credit:
                position += 1
                db.add(
                    JournalLine(
                        journal_id=journal.id,
                        position=position,
                        account_code=account.code,
                        account_id=account.id,
                        debit=debit,
                        credit=credit,
                    )
                )
        db.flush()
        last = db.scalars(
            select(FiscalPeriod).where(FiscalPeriod.year_id == year.id, FiscalPeriod.number == 12)
        ).one()
        journal.number = numbering.next_number(db, SEQUENCE)
        journal.period_id, journal.status = last.id, "posted"
        journal.posted_by, journal.posted_at = actor.user_id, clock.now()
        journal_id = journal.id
    year.status, year.closed_by, year.closed_at = "closed", actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "finance.year.close",
        entity_type="fiscal_year",
        entity_id=str(year.id),
        changes={"status": {"from": "open", "to": "closed"}},
    )
    return journal_id


# ------------------------------------------------------------------ views


def counts(db: Session) -> dict[str, int]:
    return dict(db.execute(select(Journal.status, func.count()).group_by(Journal.status)).all())


def user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def list_journals(
    db: Session,
    actor: Actor,
    tab: str,
    *,
    source: str = "",
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
) -> list[JournalView]:
    if not _can(db, actor, READ):
        raise PermissionDenied()
    query = select(Journal).where(Journal.status == (tab if tab in TABS else "posted"))
    if source in SOURCE_LABELS:
        query = query.where(Journal.source == source)
    if start:
        query = query.where(Journal.journal_date >= start)
    if end:
        query = query.where(Journal.journal_date <= end)
    rows = list(
        db.scalars(
            query.order_by(Journal.journal_date.desc(), Journal.created_at.desc()).limit(limit)
        )
    )
    totals = (
        dict(
            db.execute(
                select(JournalLine.journal_id, func.sum(JournalLine.debit))
                .where(JournalLine.journal_id.in_([r.id for r in rows]))
                .group_by(JournalLine.journal_id)
            ).all()
        )
        if rows
        else {}
    )
    who = user_names(db, {u for r in rows for u in (r.created_by, r.posted_by)})
    return [
        JournalView(
            id=r.id,
            number=r.number,
            journal_date=r.journal_date,
            original_date=r.original_date,
            memo=r.memo,
            source=r.source,
            status=r.status,
            hold_reason=r.hold_reason,
            redate_reason=r.redate_reason,
            created_by=who.get(r.created_by) if r.created_by else None,
            created_at=r.created_at,
            posted_by=who.get(r.posted_by) if r.posted_by else None,
            posted_at=r.posted_at,
            total=totals.get(r.id) or ZERO,
        )
        for r in rows
    ]


def get_journal(db: Session, actor: Actor, journal_id: uuid.UUID) -> JournalView:
    if not _can(db, actor, READ):
        raise NotFound()
    r = db.get(Journal, journal_id)
    if r is None:
        raise NotFound()
    lines = _lines(db, r.id)
    found = accounts.by_codes(db, {line.account_code for line in lines})
    centres = dict(org.cost_centre_choices(db))
    who = user_names(db, {r.created_by, r.posted_by})
    numbers = (
        dict(
            db.execute(
                select(Journal.id, Journal.number).where(
                    Journal.id.in_([i for i in (r.reverses_id, r.reversed_by_id) if i])
                )
            ).all()
        )
        if (r.reverses_id or r.reversed_by_id)
        else {}
    )
    can_post = r.status in {"draft", "held"} and _can(db, actor, POST)
    blocked = None
    if can_post and _sod_blocked(db, actor, r):
        can_post, blocked = False, "You drafted this journal, so someone else posts it."
    elif can_post:
        blocked = _problem(db, r, lines)
    return JournalView(
        id=r.id,
        number=r.number,
        journal_date=r.journal_date,
        original_date=r.original_date,
        memo=r.memo,
        source=r.source,
        status=r.status,
        hold_reason=r.hold_reason,
        redate_reason=r.redate_reason,
        created_by=who.get(r.created_by) if r.created_by else None,
        created_at=r.created_at,
        posted_by=who.get(r.posted_by) if r.posted_by else None,
        posted_at=r.posted_at,
        total=sum((line.debit for line in lines), ZERO),
        reverses=(r.reverses_id, numbers.get(r.reverses_id) or "") if r.reverses_id else None,
        reversed_by=(r.reversed_by_id, numbers.get(r.reversed_by_id) or "")
        if r.reversed_by_id
        else None,
        lines=[
            LineView(
                account_code=line.account_code,
                account_name=found[line.account_code].name if line.account_code in found else None,
                cost_centre=centres.get(line.cost_centre_id) if line.cost_centre_id else None,
                description=line.description,
                debit=line.debit,
                credit=line.credit,
                account_id=found[line.account_code].id if line.account_code in found else None,
                cost_centre_id=line.cost_centre_id,
            )
            for line in lines
        ],
        can_edit=r.status == "draft" and _can(db, actor, CREATE),
        can_post=can_post and blocked is None,
        post_blocked=blocked if r.status in {"draft", "held"} else None,
        can_reverse=r.status == "posted"
        and not r.reversed_by_id
        and r.source != "year_end"
        and _can(db, actor, POST),
    )
