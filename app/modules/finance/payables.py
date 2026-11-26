"""What is owed to suppliers (ADR 0025): bill balances after posted payments and debit
notes, open bills, and aging by days past due. Read-only."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.finance.models import Bill, Supplier, SupplierPayment, SupplierPaymentLine
from app.modules.finance.receivables import BUCKETS, bucket

ZERO = Decimal(0)


def paid_amounts(
    db: Session, bill_ids: list[uuid.UUID], *, as_at: date | None = None
) -> dict[uuid.UUID, Decimal]:
    """Settled by posted payments (WHT included) and issued debit notes, by bill."""
    if not bill_ids:
        return {}
    out: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    paid = (
        select(SupplierPaymentLine.bill_id, func.sum(SupplierPaymentLine.amount))
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentLine.payment_id)
        .where(SupplierPaymentLine.bill_id.in_(bill_ids), SupplierPayment.status == "posted")
        .group_by(SupplierPaymentLine.bill_id)
    )
    noted = (
        select(Bill.bill_id, func.sum(Bill.total))
        .where(Bill.kind == "debit_note", Bill.status == "posted", Bill.bill_id.in_(bill_ids))
        .group_by(Bill.bill_id)
    )
    if as_at:
        paid = paid.where(SupplierPayment.payment_date <= as_at)
        noted = noted.where(Bill.bill_date <= as_at)
    for bill_id, amount in [*db.execute(paid).all(), *db.execute(noted).all()]:
        if bill_id:
            out[bill_id] += amount
    return dict(out)


@dataclass(frozen=True, slots=True)
class OpenBill:
    id: uuid.UUID
    number: str
    supplier_reference: str | None
    bill_date: date
    due_date: date
    net: Decimal
    total: Decimal
    paid: Decimal
    wht_type_id: uuid.UUID | None
    days_overdue: int

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


def _posted_bills(db: Session, supplier_ids: list[uuid.UUID] | None, as_at: date) -> list[Bill]:
    query = select(Bill).where(
        Bill.kind == "bill", Bill.status == "posted", Bill.bill_date <= as_at
    )
    if supplier_ids is not None:
        query = query.where(Bill.supplier_id.in_(supplier_ids))
    return list(db.scalars(query.order_by(Bill.due_date, Bill.number)))


def open_bills(
    db: Session, supplier_id: uuid.UUID, *, as_at: date, historic: bool = False
) -> list[OpenBill]:
    """Posted bills with something left to pay, oldest due first."""
    bills = _posted_bills(db, [supplier_id], as_at)
    paid = paid_amounts(db, [b.id for b in bills], as_at=as_at if historic else None)
    rows = []
    for b in bills:
        settled = paid.get(b.id, ZERO)
        if settled < b.total:
            due = b.due_date or b.bill_date
            rows.append(
                OpenBill(
                    b.id,
                    b.number or "",
                    b.supplier_reference,
                    b.bill_date,
                    due,
                    b.net,
                    b.total,
                    settled,
                    b.wht_type_id,
                    (as_at - due).days,
                )
            )
    return rows


def balances(
    db: Session, supplier_ids: list[uuid.UUID], *, today: date
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """(owed, overdue) for each supplier, for lists."""
    bills = _posted_bills(db, supplier_ids, today)
    paid = paid_amounts(db, [b.id for b in bills])
    owed: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    overdue: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for b in bills:
        left = b.total - paid.get(b.id, ZERO)
        owed[b.supplier_id] += left
        if left > 0 and (b.due_date or b.bill_date) < today:
            overdue[b.supplier_id] += left
    return {sid: (owed[sid], overdue[sid]) for sid in supplier_ids}


@dataclass(frozen=True, slots=True)
class Summary:
    owed: Decimal
    overdue: Decimal
    wht_this_year: Decimal
    buckets: list[Decimal]


def supplier_summary(db: Session, supplier_id: uuid.UUID, *, today: date) -> Summary:
    bills = open_bills(db, supplier_id, as_at=today)
    buckets = [ZERO] * len(BUCKETS)
    for b in bills:
        buckets[bucket(b.days_overdue)] += b.balance
    wht = db.scalar(
        select(func.coalesce(func.sum(SupplierPayment.wht), 0)).where(
            SupplierPayment.supplier_id == supplier_id,
            SupplierPayment.status == "posted",
            SupplierPayment.payment_date >= today.replace(month=1, day=1),
        )
    )
    return Summary(
        sum((b.balance for b in bills), ZERO), sum(buckets[1:], ZERO), wht or ZERO, buckets
    )


@dataclass(frozen=True, slots=True)
class AgingRow:
    supplier_id: uuid.UUID
    code: str
    name: str
    buckets: list[Decimal]
    bills: list[OpenBill] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum(self.buckets, ZERO)


@dataclass(frozen=True, slots=True)
class AgingReport:
    as_at: date
    rows: list[AgingRow]

    @property
    def totals(self) -> list[Decimal]:
        return [sum((r.buckets[i] for r in self.rows), ZERO) for i in range(len(BUCKETS))]

    @property
    def total(self) -> Decimal:
        return sum((r.total for r in self.rows), ZERO)


def aging(db: Session, *, as_at: date) -> AgingReport:
    """Unpaid posted bills by days past due as at the date, by supplier. Reversed payments
    drop out entirely."""
    rows = []
    for supplier in db.scalars(select(Supplier).order_by(Supplier.name)):
        bills = open_bills(db, supplier.id, as_at=as_at, historic=True)
        if not bills:
            continue
        buckets = [ZERO] * len(BUCKETS)
        for b in bills:
            buckets[bucket(b.days_overdue)] += b.balance
        rows.append(AgingRow(supplier.id, supplier.code, supplier.name, buckets, bills))
    return AgingReport(as_at, rows)
