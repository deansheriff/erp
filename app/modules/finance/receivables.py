"""What customers owe (ADR 0024): invoice balances from allocations, unallocated credit,
aging by days past due, and statements. Read-only."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.finance.models import Allocation, Customer, Receipt, SalesDocument

ZERO = Decimal(0)
BUCKETS = ("Current", "1–30 days", "31–60 days", "61–90 days", "90+ days")  # noqa: RUF001  (dashes in ranges)
BUCKET_LIMITS = (0, 30, 60, 90)


def bucket(days_overdue: int) -> int:
    for i, limit in enumerate(BUCKET_LIMITS):
        if days_overdue <= limit:
            return i
    return len(BUCKET_LIMITS)


@dataclass(frozen=True, slots=True)
class OpenInvoice:
    id: uuid.UUID
    number: str
    doc_date: date
    due_date: date
    total: Decimal
    paid: Decimal
    days_overdue: int

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


@dataclass(frozen=True, slots=True)
class Credit:
    kind: str  # receipt or credit_note
    id: uuid.UUID
    number: str
    day: date
    total: Decimal
    used: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.total - self.used


def paid_amounts(
    db: Session, invoice_ids: list[uuid.UUID], *, as_at: date | None = None
) -> dict[uuid.UUID, Decimal]:
    if not invoice_ids:
        return {}
    query = (
        select(Allocation.invoice_id, func.sum(Allocation.amount))
        .where(Allocation.invoice_id.in_(invoice_ids))
        .group_by(Allocation.invoice_id)
    )
    if as_at:
        query = query.where(Allocation.allocated_on <= as_at)
    return dict(db.execute(query).all())


def _issued_invoices(
    db: Session, customer_ids: list[uuid.UUID] | None, as_at: date
) -> list[SalesDocument]:
    query = select(SalesDocument).where(
        SalesDocument.kind == "invoice",
        SalesDocument.status == "issued",
        SalesDocument.doc_date <= as_at,
    )
    if customer_ids is not None:
        query = query.where(SalesDocument.customer_id.in_(customer_ids))
    return list(db.scalars(query.order_by(SalesDocument.due_date, SalesDocument.number)))


def open_invoices(
    db: Session, customer_id: uuid.UUID, *, as_at: date, historic: bool = False
) -> list[OpenInvoice]:
    """Issued invoices with something left to pay, oldest due first."""
    invoices = _issued_invoices(db, [customer_id], as_at)
    paid = paid_amounts(db, [i.id for i in invoices], as_at=as_at if historic else None)
    rows = []
    for inv in invoices:
        settled = paid.get(inv.id, ZERO)
        if settled < inv.total:
            due = inv.due_date or inv.doc_date
            rows.append(
                OpenInvoice(
                    inv.id,
                    inv.number or "",
                    inv.doc_date,
                    due,
                    inv.total,
                    settled,
                    (as_at - due).days,
                )
            )
    return rows


def _used(
    db: Session, column: Any, ids: list[uuid.UUID], as_at: date | None
) -> dict[uuid.UUID, Decimal]:
    if not ids:
        return {}
    query = select(column, func.sum(Allocation.amount)).where(column.in_(ids)).group_by(column)
    if as_at:
        query = query.where(Allocation.allocated_on <= as_at)
    return dict(db.execute(query).all())


def credits(db: Session, customer_id: uuid.UUID, *, as_at: date | None = None) -> list[Credit]:
    """Receipts and issued credit notes with some amount not yet set against an invoice."""
    receipt_query = select(Receipt).where(
        Receipt.customer_id == customer_id, Receipt.status == "posted"
    )
    note_query = select(SalesDocument).where(
        SalesDocument.customer_id == customer_id,
        SalesDocument.kind == "credit_note",
        SalesDocument.status == "issued",
    )
    if as_at:
        receipt_query = receipt_query.where(Receipt.receipt_date <= as_at)
        note_query = note_query.where(SalesDocument.doc_date <= as_at)
    receipts = list(db.scalars(receipt_query))
    notes = list(db.scalars(note_query))
    used_r = _used(db, Allocation.receipt_id, [r.id for r in receipts], as_at)
    used_c = _used(db, Allocation.credit_note_id, [n.id for n in notes], as_at)
    rows = [
        Credit("receipt", r.id, r.number, r.receipt_date, r.cash + r.wht, used_r.get(r.id, ZERO))
        for r in receipts
    ] + [
        Credit("credit_note", n.id, n.number or "", n.doc_date, n.total, used_c.get(n.id, ZERO))
        for n in notes
    ]
    return sorted((c for c in rows if c.remaining > 0), key=lambda c: (c.day, c.number))


@dataclass(frozen=True, slots=True)
class Summary:
    balance: Decimal
    overdue: Decimal
    unallocated: Decimal
    wht_awaiting: Decimal
    buckets: list[Decimal]


def customer_summary(db: Session, customer_id: uuid.UUID, *, today: date) -> Summary:
    invoices = open_invoices(db, customer_id, as_at=today)
    unallocated = sum((c.remaining for c in credits(db, customer_id)), ZERO)
    buckets = [ZERO] * len(BUCKETS)
    for inv in invoices:
        buckets[bucket(inv.days_overdue)] += inv.balance
    wht = db.scalar(
        select(func.coalesce(func.sum(Receipt.wht), 0)).where(
            Receipt.customer_id == customer_id,
            Receipt.status == "posted",
            Receipt.wht > 0,
            Receipt.wht_certificate_number.is_(None),
        )
    )
    owed = sum((i.balance for i in invoices), ZERO)
    return Summary(owed - unallocated, sum(buckets[1:], ZERO), unallocated, wht or ZERO, buckets)


# ------------------------------------------------------------------ aging


@dataclass(frozen=True, slots=True)
class AgingRow:
    customer_id: uuid.UUID
    code: str
    name: str
    buckets: list[Decimal]
    credit: Decimal  # unallocated, shown as a deduction
    last_reminder: date | None
    invoices: list[OpenInvoice] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum(self.buckets, ZERO) - self.credit


@dataclass(frozen=True, slots=True)
class AgingReport:
    as_at: date
    rows: list[AgingRow]

    @property
    def totals(self) -> list[Decimal]:
        return [sum((r.buckets[i] for r in self.rows), ZERO) for i in range(len(BUCKETS))]

    @property
    def credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), ZERO)

    @property
    def total(self) -> Decimal:
        return sum((r.total for r in self.rows), ZERO)


def aging(db: Session, *, as_at: date) -> AgingReport:
    """Open invoices by days past due as at the date, by customer. Reversed receipts
    drop out entirely."""
    customers = list(db.scalars(select(Customer).order_by(Customer.name)))
    rows = []
    for customer in customers:
        invoices = open_invoices(db, customer.id, as_at=as_at, historic=True)
        credit = sum((c.remaining for c in credits(db, customer.id, as_at=as_at)), ZERO)
        if not invoices and not credit:
            continue
        buckets = [ZERO] * len(BUCKETS)
        for inv in invoices:
            buckets[bucket(inv.days_overdue)] += inv.balance
        reminders = db.scalar(
            select(func.max(SalesDocument.last_reminder_on)).where(
                SalesDocument.customer_id == customer.id
            )
        )
        last = max((d for d in (reminders, customer.last_reminder_on) if d), default=None)
        rows.append(
            AgingRow(customer.id, customer.code, customer.name, buckets, credit, last, invoices)
        )
    return AgingReport(as_at, rows)


# ------------------------------------------------------------------ statements


@dataclass(frozen=True, slots=True)
class StatementLine:
    day: date
    kind: str  # invoice, credit_note, receipt, reversal
    ref_id: uuid.UUID
    number: str
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class Statement:
    customer: Customer
    start: date
    end: date
    opening: Decimal
    lines: list[StatementLine]

    @property
    def closing(self) -> Decimal:
        return self.lines[-1].balance if self.lines else self.opening


Movement = tuple[date, int, str, uuid.UUID, str, str, Decimal, Decimal]


def _movements(db: Session, customer_id: uuid.UUID, end: date) -> list[Movement]:
    """(day, order, kind, id, number, description, debit, credit) up to the end date."""
    moves: list[Movement] = []
    for doc in db.scalars(
        select(SalesDocument).where(
            SalesDocument.customer_id == customer_id,
            SalesDocument.kind.in_(("invoice", "credit_note")),
            SalesDocument.status == "issued",
            SalesDocument.doc_date <= end,
        )
    ):
        text = doc.memo or doc.reference or ""
        if doc.kind == "invoice":
            moves.append(
                (doc.doc_date, 0, "invoice", doc.id, doc.number or "", text, doc.total, ZERO)
            )
        else:
            moves.append(
                (doc.doc_date, 1, "credit_note", doc.id, doc.number or "", text, ZERO, doc.total)
            )
    for rec in db.scalars(
        select(Receipt).where(Receipt.customer_id == customer_id, Receipt.receipt_date <= end)
    ):
        text = "Payment received" + (f", including WHT {rec.wht:,.2f}" if rec.wht else "")
        moves.append(
            (rec.receipt_date, 2, "receipt", rec.id, rec.number, text, ZERO, rec.cash + rec.wht)
        )
        if rec.reversed_on and rec.reversed_on <= end:
            moves.append(
                (
                    rec.reversed_on,
                    3,
                    "reversal",
                    rec.id,
                    rec.number,
                    f"Receipt reversed: {rec.reversal_reason or ''}".strip(),
                    rec.cash + rec.wht,
                    ZERO,
                )
            )
    return sorted(moves, key=lambda m: (m[0], m[1], m[4]))


def statement(db: Session, customer: Customer, *, start: date, end: date) -> Statement:
    opening = ZERO
    lines = []
    running = ZERO
    for day, _, kind, ref, number, text, debit, credit in _movements(db, customer.id, end):
        if day < start:
            opening += debit - credit
            running = opening
            continue
        running += debit - credit
        lines.append(StatementLine(day, kind, ref, number, text, debit, credit, running))
    if not lines:
        running = opening
    return Statement(customer, start, end, opening, lines)


def balances(
    db: Session, customer_ids: list[uuid.UUID], *, today: date
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """(balance, overdue) for each customer, for lists."""
    invoices = _issued_invoices(db, customer_ids, today)
    paid = paid_amounts(db, [i.id for i in invoices])
    owed: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    overdue: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for inv in invoices:
        left = inv.total - paid.get(inv.id, ZERO)
        owed[inv.customer_id] += left
        if left > 0 and (inv.due_date or inv.doc_date) < today:
            overdue[inv.customer_id] += left
    credit: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    received = db.execute(
        select(Receipt.customer_id, func.sum(Receipt.cash + Receipt.wht))
        .where(Receipt.customer_id.in_(customer_ids), Receipt.status == "posted")
        .group_by(Receipt.customer_id)
    ).all()
    noted = db.execute(
        select(SalesDocument.customer_id, func.sum(SalesDocument.total))
        .where(
            SalesDocument.customer_id.in_(customer_ids),
            SalesDocument.kind == "credit_note",
            SalesDocument.status == "issued",
        )
        .group_by(SalesDocument.customer_id)
    ).all()
    used = db.execute(
        select(SalesDocument.customer_id, func.sum(Allocation.amount))
        .join(SalesDocument, SalesDocument.id == Allocation.invoice_id)
        .where(SalesDocument.customer_id.in_(customer_ids))
        .group_by(SalesDocument.customer_id)
    ).all()
    for cid, amount in [*received, *noted]:
        credit[cid] += amount
    for cid, amount in used:
        credit[cid] -= amount
    return {cid: (owed[cid] - credit[cid], overdue[cid]) for cid in customer_ids}
