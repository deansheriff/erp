"""Bank reconciliations (ADR 0026). Prepared to a statement date and closing balance,
submitted once every line up to the date is matched or has an entry and the difference
is ₦0.00, and approved by someone else: approval posts the entries and locks the lines."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, banking, journals, periods, statements
from app.modules.finance.banking import APPROVE, READ, RECONCILE, can, require
from app.modules.finance.models import (
    BankAccount,
    BankReconciliation,
    Journal,
    JournalLine,
    StatementLine,
)

ZERO = Decimal(0)
STATUS_LABELS = {"draft": "Draft", "submitted": "Awaiting approval", "approved": "Approved"}
STATUS_TONES = {"draft": "neutral", "submitted": "warning", "approved": "success"}


def _sod_blocked(db: Session, actor: Actor, prepared_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": prepared_by}, held
        )
    )


def _last_approved(db: Session, bank_id: uuid.UUID) -> BankReconciliation | None:
    return db.scalars(
        select(BankReconciliation)
        .where(
            BankReconciliation.bank_account_id == bank_id,
            BankReconciliation.status == "approved",
        )
        .order_by(BankReconciliation.statement_date.desc())
        .limit(1)
    ).first()


def _open(db: Session, bank_id: uuid.UUID) -> BankReconciliation | None:
    return db.scalars(
        select(BankReconciliation).where(
            BankReconciliation.bank_account_id == bank_id,
            BankReconciliation.status != "approved",
        )
    ).first()


def start(
    db: Session, actor: Actor, bank_id: uuid.UUID, *, statement_date: date | None, balance: str
) -> uuid.UUID:
    """Start (or change the date and balance of) the account's open reconciliation."""
    require(db, actor, RECONCILE)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        raise NotFound()
    errors: dict[str, str] = {}
    amount = _signed_amount(balance, errors)
    last = _last_approved(db, bank.id)
    if statement_date is None:
        errors["statement_date"] = "Enter the statement's closing date."
    elif last and statement_date <= last.statement_date:
        errors["statement_date"] = (
            f"Choose a date after the last reconciliation, {last.statement_date:%d %b %Y}."
        )
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert statement_date is not None and amount is not None  # noqa: S101  (checked above)
    row = _open(db, bank.id)
    if row is not None and row.status == "submitted":
        raise Conflict("The open reconciliation is awaiting approval.")
    if row is None:
        row = BankReconciliation(
            bank_account_id=bank.id, statement_date=statement_date, statement_balance=amount
        )
        db.add(row)
    row.statement_date, row.statement_balance = statement_date, amount
    row.prepared_by = actor.user_id
    db.flush()
    record_by(
        db,
        actor,
        "finance.reconciliation.prepare",
        entity_type="bank_reconciliation",
        entity_id=str(row.id),
        changes={"date": statement_date.isoformat(), "balance": str(amount)},
    )
    return row.id


def _signed_amount(text: str, errors: dict[str, str]) -> Decimal | None:
    value = text.strip().replace(",", "").replace("₦", "")
    negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
    try:
        amount = Decimal(value.strip("-()"))
    except ArithmeticError:
        amount = Decimal("NaN")
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        errors["statement_balance"] = (
            "Enter the closing balance on the statement, like 14,210,400.00."
        )
        return None
    return -amount if negative else amount


@dataclass(frozen=True, slots=True)
class Outstanding:
    journal_id: uuid.UUID
    number: str
    journal_date: date
    memo: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Figures:
    ledger: Decimal
    outstanding: list[Outstanding]
    pending: list[StatementLine]
    unmatched: list[StatementLine]
    statement_balance: Decimal

    @property
    def outstanding_total(self) -> Decimal:
        return sum((o.amount for o in self.outstanding), ZERO)

    @property
    def pending_total(self) -> Decimal:
        return sum((p.amount for p in self.pending), ZERO)

    @property
    def adjusted(self) -> Decimal:
        return self.ledger - self.outstanding_total + self.pending_total

    @property
    def difference(self) -> Decimal:
        return self.statement_balance - self.adjusted

    @property
    def balanced(self) -> bool:
        return self.difference == 0 and not self.unmatched


def figures(db: Session, rec: BankReconciliation) -> Figures:
    bank = db.get(BankAccount, rec.bank_account_id)
    assert bank is not None  # noqa: S101  (foreign key)
    ledger = banking.ledger_balance(db, bank.account_id, as_at=rec.statement_date)
    matched = select(StatementLine.journal_line_id).where(
        StatementLine.journal_line_id.is_not(None)
    )
    outstanding = [
        Outstanding(j.id, j.number or "", j.journal_date, j.memo, jl.debit - jl.credit)
        for jl, j in db.execute(
            select(JournalLine, Journal)
            .join(Journal, Journal.id == JournalLine.journal_id)
            .where(
                Journal.status == "posted",
                JournalLine.account_id == bank.account_id,
                Journal.journal_date <= rec.statement_date,
                JournalLine.id.not_in(matched),
            )
            .order_by(Journal.journal_date, Journal.number)
        ).all()
    ]
    open_lines = list(
        db.scalars(
            select(StatementLine)
            .where(
                StatementLine.bank_account_id == bank.id,
                StatementLine.line_date <= rec.statement_date,
                StatementLine.reconciliation_id.is_(None),
            )
            .order_by(StatementLine.line_date)
        )
    )
    pending = [line for line in open_lines if line.status == "entry" and not line.entry_journal_id]
    unmatched = [line for line in open_lines if line.status == "unmatched"]
    return Figures(ledger, outstanding, pending, unmatched, rec.statement_balance)


def _locked(db: Session, rec_id: uuid.UUID) -> BankReconciliation:
    row = db.scalars(
        select(BankReconciliation).where(BankReconciliation.id == rec_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def submit(db: Session, actor: Actor, rec_id: uuid.UUID) -> None:
    require(db, actor, RECONCILE)
    rec = _locked(db, rec_id)
    if rec.status != "draft":
        raise Conflict("Only a draft reconciliation can be submitted.")
    found = figures(db, rec)
    if found.unmatched:
        count = len(found.unmatched)
        raise Conflict(f"{count} statement lines up to the date need matching or an entry.")
    if found.difference:
        raise Conflict(f"The difference is {found.difference:,.2f}. It must be 0.00 to submit.")
    rec.status, rec.submitted_at, rec.prepared_by = "submitted", clock.now(), actor.user_id
    db.flush()
    record_by(
        db,
        actor,
        "finance.reconciliation.submit",
        entity_type="bank_reconciliation",
        entity_id=str(rec.id),
    )


def send_back(db: Session, actor: Actor, rec_id: uuid.UUID) -> None:
    """Return a submitted reconciliation to draft, for the preparer to change."""
    require(db, actor, APPROVE)
    rec = _locked(db, rec_id)
    if rec.status != "submitted":
        raise Conflict("Only a reconciliation awaiting approval can be sent back.")
    rec.status, rec.submitted_at = "draft", None
    db.flush()
    record_by(
        db,
        actor,
        "finance.reconciliation.send_back",
        entity_type="bank_reconciliation",
        entity_id=str(rec.id),
    )


def approve(db: Session, actor: Actor, rec_id: uuid.UUID) -> None:
    """Post the entries, lock every line up to the date, and approve."""
    require(db, actor, APPROVE)
    rec = _locked(db, rec_id)
    if rec.status != "submitted":
        raise Conflict("Only a reconciliation awaiting approval can be approved.")
    if _sod_blocked(db, actor, rec.prepared_by):
        raise PermissionDenied("You prepared this reconciliation, so someone else approves it.")
    found = figures(db, rec)
    if not found.balanced:
        raise Conflict(
            "The figures have changed since it was submitted. Send it back to be redone."
        )
    for line in found.pending:
        _, closed = periods.period_for(db, line.line_date)
        if closed:
            raise Conflict(f"An entry on {line.line_date:%d %b %Y} can't post: {closed}")
        statements.post_entry(db, line, actor.user_id)
    for line in db.scalars(
        select(StatementLine).where(
            StatementLine.bank_account_id == rec.bank_account_id,
            StatementLine.line_date <= rec.statement_date,
            StatementLine.reconciliation_id.is_(None),
        )
    ):
        line.reconciliation_id = rec.id
    rec.status, rec.approved_by, rec.approved_at = "approved", actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "finance.reconciliation.approve",
        entity_type="bank_reconciliation",
        entity_id=str(rec.id),
        changes={"entries_posted": len(found.pending), "balance": str(rec.statement_balance)},
    )


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    id: uuid.UUID
    bank_account_id: uuid.UUID
    bank: str
    statement_date: date
    statement_balance: Decimal
    status: str
    prepared_by: str | None
    submitted_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None
    figures: Figures
    entry_accounts: dict[uuid.UUID, str] = field(default_factory=dict)
    can_edit: bool = False
    can_approve: bool = False
    approve_blocked: str | None = None


def get(db: Session, actor: Actor, rec_id: uuid.UUID) -> ReconciliationView:
    require(db, actor, READ)
    rec = db.get(BankReconciliation, rec_id)
    if rec is None:
        raise NotFound()
    ledger = banking.ledger_of(db, rec.bank_account_id)
    names = journals.user_names(db, {rec.prepared_by, rec.approved_by})
    found = figures(db, rec)
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    can_approve = rec.status == "submitted" and can(db, actor, APPROVE)
    blocked = None
    if can_approve and _sod_blocked(db, actor, rec.prepared_by):
        can_approve, blocked = (
            False,
            "You prepared this reconciliation, so someone else approves it.",
        )
    return ReconciliationView(
        id=rec.id,
        bank_account_id=rec.bank_account_id,
        bank=f"{ledger.code} {ledger.name}",
        statement_date=rec.statement_date,
        statement_balance=rec.statement_balance,
        status=rec.status,
        prepared_by=names.get(rec.prepared_by) if rec.prepared_by else None,
        submitted_at=rec.submitted_at,
        approved_by=names.get(rec.approved_by) if rec.approved_by else None,
        approved_at=rec.approved_at,
        figures=found,
        entry_accounts={
            line.id: codes.get(line.entry_account_id, "")
            for line in found.pending
            if line.entry_account_id
        },
        can_edit=rec.status == "draft" and can(db, actor, RECONCILE),
        can_approve=can_approve,
        approve_blocked=blocked,
    )


def for_account(db: Session, bank_id: uuid.UUID) -> list[BankReconciliation]:
    return list(
        db.scalars(
            select(BankReconciliation)
            .where(BankReconciliation.bank_account_id == bank_id)
            .order_by(BankReconciliation.statement_date.desc())
            .limit(24)
        )
    )


def open_for(db: Session, bank_id: uuid.UUID) -> BankReconciliation | None:
    return _open(db, bank_id)


def count_open(db: Session) -> int:
    return db.scalar(select(func.count()).where(BankReconciliation.status == "submitted")) or 0
