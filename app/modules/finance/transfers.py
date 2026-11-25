"""Transfers between the organisation's bank accounts (ADR 0026): recorded by one person,
approved by another, which posts debit the receiving account and credit the paying one."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import banking, journals, periods
from app.modules.finance.banking import APPROVE, READ, RECONCILE, can, require, uuid_or_none
from app.modules.finance.models import BankAccount, BankTransfer, Journal

ZERO = Decimal(0)
STATUS_LABELS = {"draft": "Awaiting approval", "posted": "Posted"}
STATUS_TONES = {"draft": "warning", "posted": "success"}


def _sod_blocked(db: Session, actor: Actor, created_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": created_by}, held
        )
    )


def record(
    db: Session,
    actor: Actor,
    *,
    from_id: str,
    to_id: str,
    amount: str,
    on: date | None,
    reference: str,
) -> uuid.UUID:
    require(db, actor, RECONCILE)
    errors: dict[str, str] = {}
    active = {b for b, _ in banking.choices(db)}
    source, target = uuid_or_none(from_id), uuid_or_none(to_id)
    if source not in active:
        errors["from_id"] = "Choose the account it leaves."
    if target not in active:
        errors["to_id"] = "Choose the account it goes to."
    elif source == target:
        errors["to_id"] = "Choose a different account."
    value = journals.parse_amount(amount, "amount", errors)
    if not errors and value <= 0:
        errors["amount"] = "Enter the amount transferred."
    if on is None:
        errors["on"] = "Enter the transfer date."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert source is not None and target is not None and on is not None  # noqa: S101
    row = BankTransfer(
        from_account_id=source,
        to_account_id=target,
        amount=value,
        transfer_date=on,
        reference=reference.strip()[:80] or None,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "finance.transfer.record",
        entity_type="bank_transfer",
        entity_id=str(row.id),
        changes={"amount": str(value)},
    )
    return row.id


def _locked(db: Session, transfer_id: uuid.UUID) -> BankTransfer:
    row = db.scalars(
        select(BankTransfer).where(BankTransfer.id == transfer_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def delete(db: Session, actor: Actor, transfer_id: uuid.UUID) -> None:
    require(db, actor, RECONCILE)
    row = _locked(db, transfer_id)
    if row.status != "draft":
        raise Conflict("A posted transfer can't be deleted.")
    record_by(
        db, actor, "finance.transfer.delete", entity_type="bank_transfer", entity_id=str(row.id)
    )
    db.delete(row)
    db.flush()


def approve(db: Session, actor: Actor, transfer_id: uuid.UUID) -> str:
    require(db, actor, APPROVE)
    row = _locked(db, transfer_id)
    if row.status != "draft":
        raise Conflict("This transfer has already been approved.")
    if _sod_blocked(db, actor, row.created_by):
        raise PermissionDenied("You recorded this transfer, so someone else approves it.")
    _, closed = periods.period_for(db, row.transfer_date)
    if closed:
        raise Conflict(closed)
    paying = banking.ledger_of(db, row.from_account_id)
    receiving = banking.ledger_of(db, row.to_account_id)
    row.number = numbering.next_number(db, "bank_transfer")
    row.status, row.approved_by, row.approved_at = "posted", actor.user_id, clock.now()
    db.flush()
    journal = journals.post_document_journal(
        db,
        source_id=row.id,
        journal_date=row.transfer_date,
        memo=f"Transfer {row.number}: {paying.name} to {receiving.name}",
        lines=[
            (receiving.code, None, row.amount, ZERO),
            (paying.code, None, ZERO, row.amount),
        ],
        poster=actor.user_id,
        source="bank",
    )
    row.journal_id = journal.id
    db.flush()
    record_by(
        db,
        actor,
        "finance.transfer.approve",
        entity_type="bank_transfer",
        entity_id=str(row.id),
        changes={"number": row.number},
    )
    return row.number


@dataclass(frozen=True, slots=True)
class TransferRow:
    id: uuid.UUID
    number: str | None
    status: str
    transfer_date: date
    from_name: str
    to_name: str
    amount: Decimal
    reference: str | None
    created_by: str | None
    created_at: datetime
    journal: tuple[uuid.UUID, str] | None
    can_approve: bool
    approve_blocked: str | None
    can_delete: bool


def list_transfers(db: Session, actor: Actor) -> list[TransferRow]:
    require(db, actor, READ)
    rows = list(
        db.scalars(
            select(BankTransfer)
            .order_by(BankTransfer.transfer_date.desc(), BankTransfer.created_at.desc())
            .limit(200)
        )
    )
    names = journals.user_names(db, {r.created_by for r in rows})
    ledgers = _all_labels(db)
    approver = can(db, actor, APPROVE)
    recorder = can(db, actor, RECONCILE)
    out = []
    for r in rows:
        journal = db.get(Journal, r.journal_id) if r.journal_id else None
        blocked = None
        allowed = r.status == "draft" and approver
        if allowed and _sod_blocked(db, actor, r.created_by):
            allowed, blocked = False, "You recorded this, so someone else approves it."
        out.append(
            TransferRow(
                r.id,
                r.number,
                r.status,
                r.transfer_date,
                ledgers.get(r.from_account_id, ""),
                ledgers.get(r.to_account_id, ""),
                r.amount,
                r.reference,
                names.get(r.created_by) if r.created_by else None,
                r.created_at,
                (journal.id, journal.number) if journal and journal.number else None,
                allowed,
                blocked,
                r.status == "draft" and recorder,
            )
        )
    return out


def _all_labels(db: Session) -> dict[uuid.UUID, str]:
    out = {}
    for bank in db.scalars(select(BankAccount)):
        ledger = banking.ledger_of(db, bank.id)
        out[bank.id] = f"{ledger.code} {ledger.name}"
    return out
