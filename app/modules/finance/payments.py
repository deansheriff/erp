"""Supplier payments (ADR 0025). One person prepares a payment against a supplier's posted
bills and someone else approves it. WHT is deducted at payment from each bill's WHT type
at the supplier's rate (company or individual), on the part settled that isn't VAT.
Approval posts the journal; the payment then downloads as the generic bank CSV. A posted
payment is reversed with a reason, never edited."""

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import bills, journals, payables, periods, receipts
from app.modules.finance.models import (
    Account,
    Bill,
    Journal,
    Supplier,
    SupplierPayment,
    SupplierPaymentLine,
    WhtType,
)

READ = "finance.purchase.read"
CREATE = "finance.payment.create"
APPROVE = "finance.payment.approve"
ZERO = Decimal(0)
MIN_REASON = 5
STATUS_LABELS = {"draft": "Awaiting approval", "posted": "Posted", "reversed": "Reversed"}
STATUS_TONES = {"draft": "warning", "posted": "success", "reversed": "neutral"}


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not _can(db, actor, permission):
        raise PermissionDenied()


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _sod_blocked(db: Session, actor: Actor, created_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": created_by}, held
        )
    )


def wht_rate(supplier: Supplier, wht_type: WhtType | None) -> Decimal | None:
    """The supplier's rate for the type, or None when the type has no rate for them yet.
    No type means no WHT (rate 0)."""
    if wht_type is None:
        return ZERO
    return wht_type.individual_rate if supplier.kind == "individual" else wht_type.company_rate


@dataclass(frozen=True, slots=True)
class PaymentInput:
    supplier_id: str
    payment_date: date | None
    bank_account_id: str
    reference: str
    allocations: list[tuple[str, str]]  # (bill id, amount settled)


@dataclass(frozen=True, slots=True)
class Allocation:
    bill_id: uuid.UUID
    amount: Decimal
    wht_type_id: uuid.UUID | None
    wht_rate: Decimal
    wht: Decimal


def _allocations(
    db: Session,
    supplier: Supplier,
    rows: list[tuple[str, str]],
    errors: dict[str, str],
    *,
    as_at: date,
) -> list[Allocation]:
    open_ = {b.id: b for b in payables.open_bills(db, supplier.id, as_at=as_at)}
    types = {t.id: t for t in db.scalars(select(WhtType))}
    out = []
    for bill_id, text in rows:
        key = f"alloc_{bill_id}"
        amount = journals.parse_amount(text, key, errors)
        if not amount:
            continue
        bill = open_.get(_uuid(bill_id))  # type: ignore[arg-type]
        if bill is None:
            errors[key] = "This bill isn't open for this supplier."
            continue
        if amount > bill.balance:
            errors[key] = f"More than the {bill.balance:,.2f} left on {bill.number}."
            continue
        wht_type = types.get(bill.wht_type_id) if bill.wht_type_id else None
        rate = wht_rate(supplier, wht_type)
        if rate is None:
            assert wht_type is not None  # noqa: S101  (no rate means a type)
            errors[key] = (
                f"{wht_type.name} has no WHT rate for {supplier.kind} suppliers yet. "
                "Enter it in Tax settings."
            )
            continue
        out.append(
            Allocation(
                bill.id,
                amount,
                bill.wht_type_id,
                rate,
                bills.wht_amount(amount, bill.net, bill.total, rate),
            )
        )
    return out


def _locked(db: Session, payment_id: uuid.UUID) -> SupplierPayment:
    row = db.scalars(
        select(SupplierPayment).where(SupplierPayment.id == payment_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def _lines(db: Session, payment_id: uuid.UUID) -> list[SupplierPaymentLine]:
    return list(
        db.scalars(select(SupplierPaymentLine).where(SupplierPaymentLine.payment_id == payment_id))
    )


def prepare(
    db: Session, actor: Actor, payment_id: uuid.UUID | None, data: PaymentInput
) -> uuid.UUID:
    """Create or change a payment awaiting approval."""
    _require(db, actor, CREATE)
    row = _locked(db, payment_id) if payment_id else None
    if row is not None and row.status != "draft":
        raise Conflict("Only a payment awaiting approval can be changed.")
    errors: dict[str, str] = {}
    supplier = db.get(Supplier, _uuid(data.supplier_id)) if _uuid(data.supplier_id) else None
    if supplier is None or not supplier.is_active:
        raise ValidationFailed(field_errors={"supplier_id": "Choose an active supplier."})
    if data.payment_date is None:
        errors["payment_date"] = "Enter the payment date."
    bank = {a.id: a for a in receipts.bank_accounts(db)}.get(_uuid(data.bank_account_id))  # type: ignore[arg-type]
    if bank is None:
        errors["bank_account_id"] = "Choose the bank or cash account it's paid from."
    allocations = _allocations(
        db, supplier, data.allocations, errors, as_at=data.payment_date or date.max
    )
    if not allocations and not errors:
        errors["form"] = "Enter what to pay against at least one bill."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.payment_date is not None and bank is not None  # noqa: S101  (checked above)
    settled = sum((a.amount for a in allocations), ZERO)
    wht = sum((a.wht for a in allocations), ZERO)
    if row is None:
        row = SupplierPayment(
            status="draft",
            supplier_id=supplier.id,
            payment_date=data.payment_date,
            bank_account_id=bank.id,
            settled=settled,
            wht=wht,
            cash=settled - wht,
        )
        db.add(row)
    row.supplier_id, row.payment_date, row.bank_account_id = (
        supplier.id,
        data.payment_date,
        bank.id,
    )
    row.reference = data.reference.strip()[:80] or None
    row.settled, row.wht, row.cash = settled, wht, settled - wht
    row.created_by = actor.user_id  # whoever last prepared it can't approve it
    db.flush()
    for old in _lines(db, row.id):
        db.delete(old)
    db.flush()
    for a in allocations:
        db.add(
            SupplierPaymentLine(
                payment_id=row.id,
                bill_id=a.bill_id,
                amount=a.amount,
                wht_type_id=a.wht_type_id,
                wht_rate=a.wht_rate,
                wht=a.wht,
            )
        )
    db.flush()
    record_by(
        db,
        actor,
        "finance.payment.prepare",
        entity_type="supplier_payment",
        entity_id=str(row.id),
        changes={"settled": str(settled), "wht": str(wht), "bills": len(allocations)},
    )
    return row.id


def delete(db: Session, actor: Actor, payment_id: uuid.UUID) -> None:
    _require(db, actor, CREATE)
    row = _locked(db, payment_id)
    if row.status != "draft":
        raise Conflict("Only a payment awaiting approval can be deleted.")
    record_by(
        db, actor, "finance.payment.delete", entity_type="supplier_payment", entity_id=str(row.id)
    )
    db.delete(row)
    db.flush()


def approve(db: Session, actor: Actor, payment_id: uuid.UUID) -> str:
    """Check the bills are still open for these amounts, post and number the payment."""
    _require(db, actor, APPROVE)
    row = _locked(db, payment_id)
    if row.status != "draft":
        raise Conflict("This payment has already been approved.")
    if _sod_blocked(db, actor, row.created_by):
        raise PermissionDenied("You prepared this payment, so someone else approves it.")
    supplier = db.get(Supplier, row.supplier_id)
    assert supplier is not None  # noqa: S101  (foreign key)
    _, closed = periods.period_for(db, row.payment_date)
    if closed:
        raise Conflict(closed)
    lines = _lines(db, row.id)
    errors: dict[str, str] = {}
    fresh = _allocations(
        db,
        supplier,
        [(str(line.bill_id), f"{line.amount:.2f}") for line in lines],
        errors,
        as_at=row.payment_date,
    )
    if errors:
        raise Conflict(
            "The bills have changed since this was prepared: " + "; ".join(errors.values())
        )
    by_bill = {a.bill_id: a for a in fresh}
    for line in lines:  # rates may have been entered or changed since
        a = by_bill[line.bill_id]
        line.wht_type_id, line.wht_rate, line.wht = a.wht_type_id, a.wht_rate, a.wht
    row.wht = sum((a.wht for a in fresh), ZERO)
    row.cash = row.settled - row.wht
    row.number = numbering.next_number(db, "supplier_payment")
    row.status, row.approved_by, row.approved_at = "posted", actor.user_id, clock.now()
    db.flush()
    settings = periods.settings(db)
    bank = db.get(Account, row.bank_account_id)
    assert bank is not None  # noqa: S101  (foreign key)
    journal_lines: list[journals.SystemLine] = [(settings.payables_code, None, row.settled, ZERO)]
    if row.cash:
        journal_lines.append((bank.code, None, ZERO, row.cash))
    if row.wht:
        journal_lines.append((settings.wht_payable_code, None, ZERO, row.wht))
    journal = journals.post_document_journal(
        db,
        source_id=row.id,
        journal_date=row.payment_date,
        memo=f"Payment {row.number} · {supplier.name}",
        lines=journal_lines,
        poster=actor.user_id,
        source="payable",
    )
    row.journal_id = journal.id
    db.flush()
    record_by(
        db,
        actor,
        "finance.payment.approve",
        entity_type="supplier_payment",
        entity_id=str(row.id),
        changes={"number": row.number, "cash": str(row.cash), "wht": str(row.wht)},
    )
    return row.number


def reverse(
    db: Session, actor: Actor, payment_id: uuid.UUID, *, on: date | None, reason: str
) -> None:
    _require(db, actor, APPROVE)
    row = _locked(db, payment_id)
    if row.status != "posted":
        raise Conflict("Only a posted payment can be reversed.")
    errors = {}
    if on is None:
        errors["reverse_on"] = "Choose the reversal date."
    elif on < row.payment_date:
        errors["reverse_on"] = "The reversal can't be before the payment."
    if len(reason.strip()) < MIN_REASON:
        errors["reason"] = "Say why it's reversed."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert on is not None and row.journal_id is not None  # noqa: S101  (checked above)
    reversal = journals.reverse_document_journal(db, row.journal_id, on=on, actor_id=actor.user_id)
    row.status, row.reversed_on = "reversed", on
    row.reversal_reason, row.reversed_by = reason.strip(), actor.user_id
    row.reversal_journal_id = reversal.id
    db.flush()
    record_by(
        db,
        actor,
        "finance.payment.reverse",
        entity_type="supplier_payment",
        entity_id=str(row.id),
        changes={"status": {"from": "posted", "to": "reversed"}, "reason": reason.strip()},
    )


def _cell(value: object) -> str:
    """Guard against spreadsheet formulas in the bank file."""
    text = str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


def bank_file(db: Session, actor: Actor, payment_id: uuid.UUID) -> tuple[str, str]:
    """The generic bank CSV for a posted payment, as payroll's bank file has it."""
    _require(db, actor, APPROVE)
    row = db.get(SupplierPayment, payment_id)
    if row is None:
        raise NotFound()
    if row.status != "posted":
        raise Conflict("Only a posted payment has a bank file.")
    supplier = db.get(Supplier, row.supplier_id)
    assert supplier is not None  # noqa: S101  (foreign key)
    if not (supplier.account_number and supplier.bank_code):
        raise Conflict("Add the supplier's bank details first.")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["Account name", "Account number", "Bank code", "Bank", "Amount", "Narration"])
    narration = f"{row.number} {row.reference or ''}".strip()
    writer.writerow(
        [
            _cell(v)
            for v in (
                supplier.account_name or supplier.name,
                supplier.account_number,
                supplier.bank_code,
                supplier.bank_name or "",
                f"{row.cash:.2f}",
                narration,
            )
        ]
    )
    record_by(
        db,
        actor,
        "finance.payment.bank_file",
        entity_type="supplier_payment",
        entity_id=str(row.id),
    )
    return f"payment-{row.number}.csv", out.getvalue()


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class LineView:
    bill_id: uuid.UUID
    number: str
    supplier_reference: str | None
    amount: Decimal
    wht_type: str | None
    wht_rate: Decimal
    wht: Decimal

    @property
    def cash(self) -> Decimal:
        return self.amount - self.wht


@dataclass(frozen=True, slots=True)
class PaymentView:
    id: uuid.UUID
    number: str | None
    status: str
    supplier_id: uuid.UUID
    supplier: str
    has_bank: bool
    payment_date: date
    bank_account_id: uuid.UUID
    bank_account: str
    reference: str | None
    settled: Decimal
    wht: Decimal
    cash: Decimal
    created_by: str | None
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    journal: tuple[uuid.UUID, str] | None
    reversal: tuple[uuid.UUID, str] | None
    reversed_on: date | None
    reversal_reason: str | None
    lines: list[LineView] = field(default_factory=list)
    can_edit: bool = False
    can_approve: bool = False
    approve_blocked: str | None = None
    can_reverse: bool = False
    can_download: bool = False


def _journal(db: Session, journal_id: uuid.UUID | None) -> tuple[uuid.UUID, str] | None:
    journal = db.get(Journal, journal_id) if journal_id else None
    return (journal.id, journal.number) if journal and journal.number else None


def get_payment(db: Session, actor: Actor, payment_id: uuid.UUID) -> PaymentView:
    _require(db, actor, READ)
    row = db.get(SupplierPayment, payment_id)
    if row is None:
        raise NotFound()
    supplier = db.get(Supplier, row.supplier_id)
    assert supplier is not None  # noqa: S101  (foreign key)
    bank = db.get(Account, row.bank_account_id)
    names = journals.user_names(db, {row.created_by, row.approved_by})
    types = {t.id: t.name for t in db.scalars(select(WhtType))}
    lines = [
        LineView(
            b.id,
            b.number or "",
            b.supplier_reference,
            line.amount,
            types.get(line.wht_type_id) if line.wht_type_id else None,
            line.wht_rate,
            line.wht,
        )
        for line, b in db.execute(
            select(SupplierPaymentLine, Bill)
            .join(Bill, Bill.id == SupplierPaymentLine.bill_id)
            .where(SupplierPaymentLine.payment_id == row.id)
            .order_by(Bill.number)
        ).all()
    ]
    draft = row.status == "draft"
    can_approve = draft and _can(db, actor, APPROVE)
    blocked = None
    if can_approve and _sod_blocked(db, actor, row.created_by):
        can_approve, blocked = False, "You prepared this payment, so someone else approves it."
    elif draft:
        _, blocked = periods.period_for(db, row.payment_date)
        can_approve = can_approve and not blocked
    approver = _can(db, actor, APPROVE)
    return PaymentView(
        id=row.id,
        number=row.number,
        status=row.status,
        supplier_id=supplier.id,
        supplier=supplier.name,
        has_bank=bool(supplier.account_number and supplier.bank_code),
        payment_date=row.payment_date,
        bank_account_id=row.bank_account_id,
        bank_account=f"{bank.code} {bank.name}" if bank else "",
        reference=row.reference,
        settled=row.settled,
        wht=row.wht,
        cash=row.cash,
        created_by=names.get(row.created_by) if row.created_by else None,
        created_at=row.created_at,
        approved_by=names.get(row.approved_by) if row.approved_by else None,
        approved_at=row.approved_at,
        journal=_journal(db, row.journal_id),
        reversal=_journal(db, row.reversal_journal_id),
        reversed_on=row.reversed_on,
        reversal_reason=row.reversal_reason,
        lines=lines,
        can_edit=draft and _can(db, actor, CREATE),
        can_approve=can_approve,
        approve_blocked=blocked,
        can_reverse=row.status == "posted" and approver,
        can_download=row.status == "posted" and approver,
    )


@dataclass(frozen=True, slots=True)
class PaymentRow:
    id: uuid.UUID
    number: str | None
    supplier: str
    payment_date: date
    settled: Decimal
    wht: Decimal
    cash: Decimal
    status: str


def list_payments(
    db: Session,
    actor: Actor,
    *,
    supplier_id: uuid.UUID | None,
    status: str,
    start: date | None,
    end: date | None,
) -> list[PaymentRow]:
    _require(db, actor, READ)
    query = select(SupplierPayment, Supplier.name).join(
        Supplier, Supplier.id == SupplierPayment.supplier_id
    )
    if supplier_id:
        query = query.where(SupplierPayment.supplier_id == supplier_id)
    if status:
        query = query.where(SupplierPayment.status == status)
    if start:
        query = query.where(SupplierPayment.payment_date >= start)
    if end:
        query = query.where(SupplierPayment.payment_date <= end)
    rows = db.execute(
        query.order_by(SupplierPayment.payment_date.desc(), SupplierPayment.number.desc()).limit(
            500
        )
    ).all()
    return [
        PaymentRow(p.id, p.number, name, p.payment_date, p.settled, p.wht, p.cash, p.status)
        for p, name in rows
    ]


def draft_values(db: Session, payment_id: uuid.UUID) -> dict[str, str]:
    """A draft payment's allocations as form values, by bill id."""
    return {str(line.bill_id): f"{line.amount:.2f}" for line in _lines(db, payment_id)}
