"""Customer receipts (ADR 0024): cash received plus any withholding tax the customer
deducted, both settling invoices. The journal posts with the receipt. Credit left over
is allocated later. A receipt is reversed with a reason, never edited."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, journals, periods, receivables
from app.modules.finance.models import (
    Account,
    Allocation,
    Customer,
    Journal,
    Receipt,
    SalesDocument,
    WhtType,
)

READ = "finance.sales.read"
CREATE = "finance.receipt.create"
REVERSE = "finance.receipt.reverse"
SEQUENCE = "receipt"
ZERO = Decimal(0)
MIN_REASON = 5


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def bank_accounts(db: Session) -> list[Account]:
    """Accounts money is paid into: active asset accounts in the cash class."""
    return [a for a in accounts.postable(db) if a.type == "asset" and a.cash_flow == "cash"]


@dataclass(frozen=True, slots=True)
class ReceiptInput:
    customer_id: str
    receipt_date: date | None
    bank_account_id: str
    reference: str
    cash: str
    wht: str
    wht_type_id: str
    allocations: list[tuple[str, str]]  # (invoice id, amount)


def _parse_allocations(
    db: Session,
    customer_id: uuid.UUID,
    rows: list[tuple[str, str]],
    errors: dict[str, str],
    *,
    as_at: date,
) -> list[tuple[uuid.UUID, Decimal]]:
    open_ = {i.id: i for i in receivables.open_invoices(db, customer_id, as_at=as_at)}
    out = []
    for invoice_id, text in rows:
        key = f"alloc_{invoice_id}"
        amount = journals.parse_amount(text, key, errors)
        if not amount:
            continue
        invoice = open_.get(_uuid(invoice_id))  # type: ignore[arg-type]
        if invoice is None:
            errors[key] = "This invoice isn't open for this customer."
        elif amount > invoice.balance:
            errors[key] = f"More than the {invoice.balance:,.2f} left on {invoice.number}."
        else:
            out.append((invoice.id, amount))
    return out


def record(db: Session, actor: Actor, data: ReceiptInput) -> uuid.UUID:
    _require(db, actor, CREATE)
    errors: dict[str, str] = {}
    customer = db.get(Customer, _uuid(data.customer_id)) if _uuid(data.customer_id) else None
    if customer is None:
        raise ValidationFailed(field_errors={"customer_id": "Choose the customer."})
    if data.receipt_date is None:
        errors["receipt_date"] = "Enter the date it was received."
    bank = {a.id: a for a in bank_accounts(db)}.get(_uuid(data.bank_account_id))  # type: ignore[arg-type]
    if bank is None:
        errors["bank_account_id"] = "Choose the bank or cash account it was paid into."
    cash = journals.parse_amount(data.cash, "cash", errors)
    wht = journals.parse_amount(data.wht, "wht", errors)
    wht_type = db.get(WhtType, _uuid(data.wht_type_id)) if _uuid(data.wht_type_id) else None
    if wht and (wht_type is None or not wht_type.is_active):
        errors["wht_type_id"] = "Choose the type of withholding tax deducted."
    if not errors and cash + wht <= 0:
        errors["cash"] = "Enter the amount received."
    if data.receipt_date and "receipt_date" not in errors:
        _, reason = periods.period_for(db, data.receipt_date)
        if reason:
            errors["receipt_date"] = reason
    allocations = _parse_allocations(
        db, customer.id, data.allocations, errors, as_at=data.receipt_date or date.max
    )
    total = cash + wht
    allocated = sum((a for _, a in allocations), ZERO)
    if not errors and allocated > total:
        errors["form"] = f"You've allocated {allocated:,.2f}, more than the {total:,.2f} received."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.receipt_date is not None and bank is not None  # noqa: S101  (checked above)
    receipt = Receipt(
        number=numbering.next_number(db, SEQUENCE),
        customer_id=customer.id,
        receipt_date=data.receipt_date,
        bank_account_id=bank.id,
        reference=data.reference.strip()[:80] or None,
        cash=cash,
        wht=wht,
        wht_type_id=wht_type.id if wht and wht_type else None,
        created_by=actor.user_id,
    )
    db.add(receipt)
    db.flush()
    settings = periods.settings(db)
    lines: list[journals.SystemLine] = [(settings.receivables_code, None, ZERO, total)]
    if cash:
        lines.append((bank.code, None, cash, ZERO))
    if wht:
        lines.append((settings.wht_receivable_code, None, wht, ZERO))
    journal = journals.post_document_journal(
        db,
        source_id=receipt.id,
        journal_date=receipt.receipt_date,
        memo=f"Receipt {receipt.number} · {customer.name}",
        lines=lines,
        poster=actor.user_id,
    )
    receipt.journal_id = journal.id
    for invoice_id, amount in allocations:
        db.add(
            Allocation(
                invoice_id=invoice_id,
                receipt_id=receipt.id,
                amount=amount,
                allocated_on=receipt.receipt_date,
                created_by=actor.user_id,
            )
        )
    db.flush()
    record_by(
        db,
        actor,
        "finance.receipt.record",
        entity_type="receipt",
        entity_id=str(receipt.id),
        changes={"number": receipt.number, "cash": str(cash), "wht": str(wht)},
    )
    return receipt.id


def allocate(
    db: Session,
    actor: Actor,
    customer_id: uuid.UUID,
    *,
    credit: str,
    invoice_id: str,
    amount: str,
    today: date,
) -> None:
    """Set unallocated credit (a receipt or credit note, `receipt:<id>` or
    `credit_note:<id>`) against one of the customer's open invoices."""
    _require(db, actor, CREATE)
    kind, _, raw = credit.partition(":")
    source = next(
        (c for c in receivables.credits(db, customer_id) if c.kind == kind and str(c.id) == raw),
        None,
    )
    errors: dict[str, str] = {}
    value = journals.parse_amount(amount, "amount", errors)
    if source is None:
        errors["credit"] = "Choose credit that's still unallocated."
    invoice = next(
        (
            i
            for i in receivables.open_invoices(db, customer_id, as_at=today)
            if str(i.id) == invoice_id
        ),
        None,
    )
    if invoice is None:
        errors["invoice_id"] = "Choose an open invoice."
    if not errors and value <= 0:
        errors["amount"] = "Enter the amount to allocate."
    if not errors and source and invoice and value > min(source.remaining, invoice.balance):
        errors["amount"] = f"Allocate {min(source.remaining, invoice.balance):,.2f} at most."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert source is not None and invoice is not None  # noqa: S101  (checked above)
    db.add(
        Allocation(
            invoice_id=invoice.id,
            receipt_id=source.id if kind == "receipt" else None,
            credit_note_id=source.id if kind == "credit_note" else None,
            amount=value,
            allocated_on=max(today, source.day, invoice.doc_date),
            created_by=actor.user_id,
        )
    )
    db.flush()
    record_by(
        db,
        actor,
        "finance.receipt.allocate",
        entity_type="sales_document",
        entity_id=str(invoice.id),
        changes={"from": source.number, "amount": str(value)},
    )


def reverse(
    db: Session, actor: Actor, receipt_id: uuid.UUID, *, on: date | None, reason: str
) -> None:
    """Undo a receipt (a bounced cheque, a wrong customer): its journal is reversed on the
    date given and the invoices it settled are open again."""
    _require(db, actor, REVERSE)
    receipt = db.scalars(select(Receipt).where(Receipt.id == receipt_id).with_for_update()).first()
    if receipt is None:
        raise NotFound()
    if receipt.status != "posted":
        raise Conflict("This receipt has already been reversed.")
    errors = {}
    if on is None:
        errors["reverse_on"] = "Choose the reversal date."
    elif on < receipt.receipt_date:
        errors["reverse_on"] = "The reversal can't be before the receipt."
    if len(reason.strip()) < MIN_REASON:
        errors["reason"] = "Say why it's reversed."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert on is not None and receipt.journal_id is not None  # noqa: S101  (checked above)
    reversal = journals.reverse_document_journal(
        db, receipt.journal_id, on=on, actor_id=actor.user_id
    )
    removed = list(db.scalars(select(Allocation).where(Allocation.receipt_id == receipt.id)))
    for alloc in removed:
        db.delete(alloc)
    receipt.status, receipt.reversed_on = "reversed", on
    receipt.reversal_reason, receipt.reversed_by = reason.strip(), actor.user_id
    receipt.reversal_journal_id = reversal.id
    db.flush()
    record_by(
        db,
        actor,
        "finance.receipt.reverse",
        entity_type="receipt",
        entity_id=str(receipt.id),
        changes={
            "status": {"from": "posted", "to": "reversed"},
            "allocations_removed": len(removed),
            "reason": reason.strip(),
        },
    )


def record_certificate(
    db: Session, actor: Actor, receipt_id: uuid.UUID, *, number: str, received_on: date | None
) -> None:
    """The customer's WHT credit note for tax they deducted from this receipt."""
    _require(db, actor, CREATE)
    receipt = db.scalars(select(Receipt).where(Receipt.id == receipt_id).with_for_update()).first()
    if receipt is None:
        raise NotFound()
    if not receipt.wht or receipt.status != "posted":
        raise Conflict("Only a receipt with withholding tax deducted needs a credit note.")
    errors = {}
    if not number.strip():
        errors["certificate_number"] = "Enter the credit note number."
    if received_on is None:
        errors["certificate_date"] = "Enter the date it was received."
    if errors:
        raise ValidationFailed(field_errors=errors)
    before = receipt.wht_certificate_number
    receipt.wht_certificate_number = number.strip()[:60]
    receipt.wht_certificate_date = received_on
    db.flush()
    record_by(
        db,
        actor,
        "finance.receipt.wht_certificate",
        entity_type="receipt",
        entity_id=str(receipt.id),
        changes={"certificate": {"from": before, "to": receipt.wht_certificate_number}},
    )


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class ReceiptView:
    id: uuid.UUID
    number: str
    status: str
    customer_id: uuid.UUID
    customer: str
    receipt_date: date
    bank_account: str
    reference: str | None
    cash: Decimal
    wht: Decimal
    wht_type: str | None
    certificate_number: str | None
    certificate_date: date | None
    journal: tuple[uuid.UUID, str] | None
    reversal: tuple[uuid.UUID, str] | None
    reversed_on: date | None
    reversal_reason: str | None
    created_by: str | None
    created_at: datetime
    allocations: list[tuple[uuid.UUID, str, Decimal]] = field(default_factory=list)
    can_reverse: bool = False
    can_record: bool = False

    @property
    def total(self) -> Decimal:
        return self.cash + self.wht

    @property
    def allocated(self) -> Decimal:
        return sum((a for _, _, a in self.allocations), ZERO)


def _journal(db: Session, journal_id: uuid.UUID | None) -> tuple[uuid.UUID, str] | None:
    journal = db.get(Journal, journal_id) if journal_id else None
    return (journal.id, journal.number) if journal and journal.number else None


def get_receipt(db: Session, actor: Actor, receipt_id: uuid.UUID) -> ReceiptView:
    _require(db, actor, READ)
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise NotFound()
    customer = db.get(Customer, receipt.customer_id)
    bank = db.get(Account, receipt.bank_account_id)
    wht_type = db.get(WhtType, receipt.wht_type_id) if receipt.wht_type_id else None
    allocations = [
        (doc.id, doc.number or "", alloc.amount)
        for alloc, doc in db.execute(
            select(Allocation, SalesDocument)
            .join(SalesDocument, SalesDocument.id == Allocation.invoice_id)
            .where(Allocation.receipt_id == receipt.id)
            .order_by(SalesDocument.number)
        ).all()
    ]
    names = journals.user_names(db, {receipt.created_by})
    posted = receipt.status == "posted"
    return ReceiptView(
        id=receipt.id,
        number=receipt.number,
        status=receipt.status,
        customer_id=receipt.customer_id,
        customer=customer.name if customer else "",
        receipt_date=receipt.receipt_date,
        bank_account=f"{bank.code} {bank.name}" if bank else "",
        reference=receipt.reference,
        cash=receipt.cash,
        wht=receipt.wht,
        wht_type=wht_type.name if wht_type else None,
        certificate_number=receipt.wht_certificate_number,
        certificate_date=receipt.wht_certificate_date,
        journal=_journal(db, receipt.journal_id),
        reversal=_journal(db, receipt.reversal_journal_id),
        reversed_on=receipt.reversed_on,
        reversal_reason=receipt.reversal_reason,
        created_by=names.get(receipt.created_by) if receipt.created_by else None,
        created_at=receipt.created_at,
        allocations=allocations,
        can_reverse=posted and bool(engine.check(db, actor.user_id, REVERSE)),
        can_record=posted and bool(engine.check(db, actor.user_id, CREATE)),
    )


@dataclass(frozen=True, slots=True)
class ReceiptRow:
    id: uuid.UUID
    number: str
    customer: str
    receipt_date: date
    cash: Decimal
    wht: Decimal
    status: str
    awaiting_certificate: bool

    @property
    def total(self) -> Decimal:
        return self.cash + self.wht


def list_receipts(
    db: Session,
    actor: Actor,
    *,
    customer_id: uuid.UUID | None,
    start: date | None,
    end: date | None,
    awaiting: bool = False,
) -> list[ReceiptRow]:
    _require(db, actor, READ)
    query = select(Receipt, Customer.name).join(Customer, Customer.id == Receipt.customer_id)
    if customer_id:
        query = query.where(Receipt.customer_id == customer_id)
    if start:
        query = query.where(Receipt.receipt_date >= start)
    if end:
        query = query.where(Receipt.receipt_date <= end)
    if awaiting:
        query = query.where(
            Receipt.status == "posted", Receipt.wht > 0, Receipt.wht_certificate_number.is_(None)
        )
    rows = db.execute(
        query.order_by(Receipt.receipt_date.desc(), Receipt.number.desc()).limit(500)
    ).all()
    return [
        ReceiptRow(
            r.id,
            r.number,
            name,
            r.receipt_date,
            r.cash,
            r.wht,
            r.status,
            bool(r.wht) and r.status == "posted" and not r.wht_certificate_number,
        )
        for r, name in rows
    ]
