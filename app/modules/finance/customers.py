"""Customers and payment reminders (ADR 0024). Reminders go by email a set number of days
before an invoice is due, on the due date, then every few days while it is overdue; an
invoice never gets two in one day. Staff can also send one from the customer page."""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.mail import service as mail
from app.modules.finance import pdf, periods, receivables, sales
from app.modules.finance.models import Customer, FinanceSettings, SalesDocument

READ = "finance.customer.read"
MANAGE = "finance.customer.manage"
SEQUENCE = "customer"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_TERMS = 365
SHOWS = ("active", "inactive", "overdue")


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()


@dataclass(frozen=True, slots=True)
class CustomerInput:
    name: str
    email: str
    phone: str
    address: str
    tin: str
    payment_terms_days: str
    reminders: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class CustomerRow:
    id: uuid.UUID
    code: str
    name: str
    email: str | None
    tin: str | None
    payment_terms_days: int
    reminders: bool
    is_active: bool
    balance: Decimal
    overdue: Decimal


def list_customers(
    db: Session, actor: Actor, *, q: str = "", show: str = "active", today: date
) -> list[CustomerRow]:
    _require(db, actor, READ)
    query = select(Customer).order_by(Customer.name)
    if show == "inactive":
        query = query.where(Customer.is_active.is_(False))
    else:
        query = query.where(Customer.is_active)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(Customer.name.ilike(like), Customer.code.ilike(like), Customer.email.ilike(like))
        )
    rows = list(db.scalars(query.limit(500)))
    money = receivables.balances(db, [c.id for c in rows], today=today)
    out = [
        CustomerRow(
            c.id,
            c.code,
            c.name,
            c.email,
            c.tin,
            c.payment_terms_days,
            c.reminders,
            c.is_active,
            *money[c.id],
        )
        for c in rows
    ]
    return [r for r in out if r.overdue > 0] if show == "overdue" else out


def choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    return [
        (c.id, f"{c.code} · {c.name}")
        for c in db.scalars(select(Customer).where(Customer.is_active).order_by(Customer.name))
    ]


def get(db: Session, actor: Actor, customer_id: uuid.UUID) -> Customer:
    _require(db, actor, READ)
    row = db.get(Customer, customer_id)
    if row is None:
        raise NotFound()
    return row


def default_terms(db: Session) -> int:
    return periods.settings(db).default_terms_days


def save(
    db: Session, actor: Actor, customer_id: uuid.UUID | None, data: CustomerInput
) -> uuid.UUID:
    _require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    name = data.name.strip()
    email = data.email.strip().lower()
    if not name:
        errors["name"] = "Enter the customer's name."
    else:
        clash = select(Customer.id).where(func.lower(Customer.name) == name.lower())
        if customer_id:
            clash = clash.where(Customer.id != customer_id)
        if db.scalar(clash):
            errors["name"] = "A customer already has that name."
    if email and not EMAIL.match(email):
        errors["email"] = "Enter an email address, like accounts@example.com."
    if data.reminders and not email:
        errors["email"] = "Reminders need an email address."
    try:
        terms = int(data.payment_terms_days.strip() or default_terms(db))
    except ValueError:
        terms = -1
    if not 0 <= terms <= MAX_TERMS:
        errors["payment_terms_days"] = f"Enter days from 0 to {MAX_TERMS}."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(Customer, customer_id) if customer_id else None
    if customer_id and row is None:
        raise NotFound()
    if row is None:
        row = Customer(
            code=numbering.next_number(db, SEQUENCE), name=name, payment_terms_days=terms
        )
        db.add(row)
    before = {
        k: getattr(row, k, None) for k in ("name", "email", "payment_terms_days", "is_active")
    }
    row.name, row.email = name, email or None
    row.phone = data.phone.strip()[:40] or None
    row.address = data.address.strip() or None
    row.tin = data.tin.strip()[:30] or None
    row.payment_terms_days, row.reminders, row.is_active = terms, data.reminders, data.is_active
    db.flush()
    after = {k: getattr(row, k) for k in before}
    record_by(
        db,
        actor,
        "finance.customer.save",
        entity_type="customer",
        entity_id=str(row.id),
        changes={k: {"from": before[k], "to": v} for k, v in after.items() if before[k] != v},
    )
    return row.id


# ------------------------------------------------------------------ reminders


def _queue(
    db: Session, customer: Customer, invoices: list[receivables.OpenInvoice], today: date
) -> None:
    rows = [
        {
            "number": inv.number,
            "due": f"{inv.due_date:%d %b %Y}",
            "balance": f"NGN {inv.balance:,.2f}",
            "overdue": max(inv.days_overdue, 0),
        }
        for inv in invoices
    ]
    total = sum((inv.balance for inv in invoices), Decimal(0))
    mail.queue(
        db,
        to=customer.email or "",
        template="finance_payment_reminder",
        context={
            "organisation": sales.organisation_name(db),
            "customer": customer.name,
            "invoices": rows,
            "total": f"NGN {total:,.2f}",
            "today": f"{today:%d %b %Y}",
        },
    )


def remind_now(db: Session, actor: Actor, customer_id: uuid.UUID, *, today: date) -> int:
    """Email the customer their open invoices now. Returns how many were listed."""
    _require(db, actor, MANAGE)
    customer = db.scalars(
        select(Customer).where(Customer.id == customer_id).with_for_update()
    ).first()
    if customer is None:
        raise NotFound()
    if not customer.email:
        raise Conflict("Add the customer's email address first.")
    if customer.last_reminder_on == today:
        raise Conflict("A reminder already went to this customer today.")
    invoices = receivables.open_invoices(db, customer.id, as_at=today)
    if not invoices:
        raise Conflict("Nothing is owed, so there's nothing to remind them about.")
    _queue(db, customer, invoices, today)
    customer.last_reminder_on = today
    for doc in db.scalars(
        select(SalesDocument).where(SalesDocument.id.in_([i.id for i in invoices]))
    ):
        doc.last_reminder_on = today
    db.flush()
    record_by(
        db,
        actor,
        "finance.customer.remind",
        entity_type="customer",
        entity_id=str(customer.id),
        changes={"invoices": len(invoices)},
    )
    return len(invoices)


def _due_for_reminder(
    invoice: receivables.OpenInvoice, today: date, settings: FinanceSettings
) -> bool:
    days = (today - invoice.due_date).days
    if days < 0:
        return settings.remind_before_days > 0 and -days == settings.remind_before_days
    if days == 0:
        return settings.remind_on_due
    return days % settings.remind_every_days == 0


def send_due(db: Session, *, today: date) -> int:
    """The nightly run: one email per customer listing the invoices due a reminder today.
    Returns how many emails were queued."""
    settings = periods.settings(db)
    sent = 0
    for customer in db.scalars(
        select(Customer).where(Customer.is_active, Customer.reminders, Customer.email.is_not(None))
    ):
        invoices = receivables.open_invoices(db, customer.id, as_at=today)
        reminded = {
            d.id: d.last_reminder_on
            for d in db.scalars(
                select(SalesDocument).where(SalesDocument.id.in_([i.id for i in invoices]))
            )
        }
        due = [
            inv
            for inv in invoices
            if reminded.get(inv.id) != today and _due_for_reminder(inv, today, settings)
        ]
        if not due:
            continue
        _queue(db, customer, due, today)
        for doc in db.scalars(
            select(SalesDocument).where(SalesDocument.id.in_([i.id for i in due]))
        ):
            doc.last_reminder_on = today
        sent += 1
    db.flush()
    if sent:
        record_by(
            db, None, "finance.reminders.sent", entity_type="customer", changes={"emails": sent}
        )
    return sent


def statement_pdf(
    db: Session, actor: Actor, customer_id: uuid.UUID, *, start: date, end: date
) -> tuple[str, bytes]:
    customer = get(db, actor, customer_id)
    view = receivables.statement(db, customer, start=start, end=end)
    summary = receivables.customer_summary(db, customer.id, today=end)
    rows = [
        (
            pdf.day(line.day),
            line.number,
            line.description,
            f"{line.debit:,.2f}" if line.debit else "",
            f"{line.credit:,.2f}" if line.credit else "",
            f"{line.balance:,.2f}",
        )
        for line in view.lines
    ]
    data = pdf.StatementPdf(
        sales.seller(db),
        sales.buyer(customer),
        f"{pdf.day(start)} to {pdf.day(end)}",
        view.opening,
        rows,
        view.closing,
        list(zip(receivables.BUCKETS, (pdf.money(b) for b in summary.buckets), strict=True)),
    )
    return f"statement-{customer.code}-{end:%Y-%m-%d}.pdf", pdf.render_statement(data)
