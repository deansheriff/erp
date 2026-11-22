"""Finance (ADR 0023): the chart of accounts, financial years and their monthly periods,
journals and their lines, recurring journal templates, and finance settings."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
CASH_FLOW_CLASSES = ("cash", "operating", "investing", "financing")
STATUSES = ("open", "closed")
JOURNAL_STATUSES = ("draft", "held", "posted")
SOURCES = ("manual", "recurring", "reversal", "year_end", "payroll", "expenses", "receivable")

Money = Numeric(16, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Account(IdMixin, Base):
    __tablename__ = "finance_accounts"
    __table_args__ = (
        CheckConstraint(_in("type", ACCOUNT_TYPES), name="type"),
        CheckConstraint(
            f"cash_flow IS NULL OR {_in('cash_flow', CASH_FLOW_CLASSES)}", name="cash_flow"
        ),
    )

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(10))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finance_accounts.id"), index=True
    )
    is_header: Mapped[bool] = mapped_column(Boolean, server_default="false")
    cash_flow: Mapped[str | None] = mapped_column(String(10))  # balance-sheet accounts
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class FiscalYear(IdMixin, Base):
    __tablename__ = "fiscal_years"
    __table_args__ = (CheckConstraint(_in("status", STATUSES), name="status"),)

    year: Mapped[int] = mapped_column(SmallInteger, unique=True)
    start_date: Mapped[date]
    end_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(6), server_default="open")
    opened_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]


class FiscalPeriod(IdMixin, Base):
    __tablename__ = "fiscal_periods"
    __table_args__ = (
        UniqueConstraint("year_id", "number"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint("number BETWEEN 1 AND 12", name="number"),
    )

    year_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fiscal_years.id"), index=True)
    number: Mapped[int] = mapped_column(SmallInteger)
    start_date: Mapped[date] = mapped_column(index=True)
    end_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(6), server_default="open")
    closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]
    reopened_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reopened_at: Mapped[datetime | None]
    reopen_reason: Mapped[str | None] = mapped_column(Text)


class RecurringJournal(IdMixin, Base):
    __tablename__ = "recurring_journals"
    __table_args__ = (CheckConstraint("day_of_month BETWEEN 1 AND 28", name="day"),)

    name: Mapped[str] = mapped_column(String(120))
    memo: Mapped[str] = mapped_column(String(200))
    day_of_month: Mapped[int] = mapped_column(SmallInteger)
    first_month: Mapped[date]
    last_month: Mapped[date | None]
    next_date: Mapped[date | None]  # None once the last month has been drafted
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RecurringLine(IdMixin, Base):
    __tablename__ = "recurring_journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="one_side"),
    )

    recurring_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recurring_journals.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    description: Mapped[str | None] = mapped_column(String(200))
    debit: Mapped[Decimal] = mapped_column(Money, server_default="0")
    credit: Mapped[Decimal] = mapped_column(Money, server_default="0")


class Journal(IdMixin, Base):
    """A journal: drafted, held (a system journal that couldn't post) or posted. Posted
    journals are never edited, only reversed."""

    __tablename__ = "journals"
    __table_args__ = (
        CheckConstraint(_in("status", JOURNAL_STATUSES), name="status"),
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint("(status = 'posted') = (number IS NOT NULL)", name="numbered"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    journal_date: Mapped[date] = mapped_column(index=True)
    original_date: Mapped[date | None]  # a held journal re-dated into an open month
    period_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fiscal_periods.id"))
    memo: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(10))
    source_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(6), server_default="draft")
    hold_reason: Mapped[str | None] = mapped_column(Text)
    redate_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reversed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    recurring_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recurring_journals.id"))


# A system document hands over its journal once.
Index(
    "uq_journal_source",
    Journal.source,
    Journal.source_id,
    unique=True,
    postgresql_where=Journal.source_id.is_not(None),
)


class JournalLine(IdMixin, Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="one_side"),
    )

    journal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journals.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    account_code: Mapped[str] = mapped_column(String(20))  # as given, for held journals
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finance_accounts.id"), index=True
    )
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    description: Mapped[str | None] = mapped_column(String(200))
    debit: Mapped[Decimal] = mapped_column(Money, server_default="0")
    credit: Mapped[Decimal] = mapped_column(Money, server_default="0")
    currency: Mapped[str] = mapped_column(String(3), server_default="NGN")


class FinanceSettings(Base):
    """One row (defaults to confirm)."""

    __tablename__ = "finance_settings"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    retained_earnings_code: Mapped[str] = mapped_column(String(20))
    # Receivables and tax (ADR 0024), all to confirm.
    receivables_code: Mapped[str] = mapped_column(String(20), server_default="1100")
    vat_output_code: Mapped[str] = mapped_column(String(20), server_default="2200")
    vat_input_code: Mapped[str] = mapped_column(String(20), server_default="1310")
    wht_receivable_code: Mapped[str] = mapped_column(String(20), server_default="1320")
    wht_payable_code: Mapped[str] = mapped_column(String(20), server_default="2210")
    stamp_duty_code: Mapped[str] = mapped_column(String(20), server_default="2220")
    default_terms_days: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    remind_before_days: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    remind_on_due: Mapped[bool] = mapped_column(Boolean, server_default="true")
    remind_every_days: Mapped[int] = mapped_column(SmallInteger, server_default="7")
    return_due_day: Mapped[int] = mapped_column(SmallInteger, server_default="21")


# ------------------------------------------------------------------ receivables and tax (ADR 0024)

DOCUMENT_KINDS = ("quote", "order", "invoice", "credit_note")
DOCUMENT_STATUSES = (
    "draft",
    "sent",
    "accepted",
    "declined",
    "converted",
    "confirmed",
    "invoiced",
    "cancelled",
    "issued",
)
VAT_CODES = ("standard", "zero", "exempt")
STAMP_BASES = ("flat", "percent")
RECEIPT_STATUSES = ("posted", "reversed")
TAXES = ("vat", "wht", "stamp")
RETURN_STATUSES = ("filed", "paid")  # a return not yet filed has no row


class Customer(IdMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("payment_terms_days BETWEEN 0 AND 365", name="terms"),)

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(Text)
    tin: Mapped[str | None] = mapped_column(String(30))
    payment_terms_days: Mapped[int] = mapped_column(SmallInteger)
    reminders: Mapped[bool] = mapped_column(Boolean, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    last_reminder_on: Mapped[date | None]  # the last reminder sent from the customer page
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StampDutyType(IdMixin, Base):
    __tablename__ = "stamp_duty_types"
    __table_args__ = (
        CheckConstraint(_in("basis", STAMP_BASES), name="basis"),
        CheckConstraint("value >= 0", name="value"),
    )

    name: Mapped[str] = mapped_column(String(120), unique=True)
    basis: Mapped[str] = mapped_column(String(7))
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4))  # naira, or a percentage
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class SalesDocument(IdMixin, Base):
    """A quote, sales order, invoice or credit note. Issued ones are never edited."""

    __tablename__ = "sales_documents"
    __table_args__ = (
        CheckConstraint(_in("kind", DOCUMENT_KINDS), name="kind"),
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
        CheckConstraint("net >= 0 AND vat >= 0 AND stamp_duty >= 0", name="amounts"),
    )

    kind: Mapped[str] = mapped_column(String(11), index=True)
    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    doc_date: Mapped[date]
    due_date: Mapped[date | None]  # invoices
    valid_until: Mapped[date | None]  # quotes
    reference: Mapped[str | None] = mapped_column(String(80))  # the customer's PO number
    memo: Mapped[str | None] = mapped_column(String(200))
    # The quote or order it was converted from.
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sales_documents.id"))
    # The invoice a credit note was raised against.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sales_documents.id"))
    stamp_duty_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stamp_duty_types.id"))
    net: Mapped[Decimal] = mapped_column(Money, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Money, server_default="0")
    stamp_duty: Mapped[Decimal] = mapped_column(Money, server_default="0")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    issued_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    last_reminder_on: Mapped[date | None]


class SalesLine(IdMixin, Base):
    __tablename__ = "sales_lines"
    __table_args__ = (
        CheckConstraint(_in("vat_code", VAT_CODES), name="vat_code"),
        CheckConstraint("quantity > 0 AND unit_price >= 0", name="amounts"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_documents.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(String(300))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    vat_code: Mapped[str] = mapped_column(String(8))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    vat: Mapped[Decimal] = mapped_column(Money)


class Receipt(IdMixin, Base):
    __tablename__ = "receipts"
    __table_args__ = (
        CheckConstraint(_in("status", RECEIPT_STATUSES), name="status"),
        CheckConstraint("cash >= 0 AND wht >= 0 AND cash + wht > 0", name="amounts"),
    )

    number: Mapped[str] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(8), server_default="posted")
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    receipt_date: Mapped[date]
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    reference: Mapped[str | None] = mapped_column(String(80))
    cash: Mapped[Decimal] = mapped_column(Money)
    wht: Mapped[Decimal] = mapped_column(Money, server_default="0")
    wht_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wht_types.id"))
    wht_certificate_number: Mapped[str | None] = mapped_column(String(60))
    wht_certificate_date: Mapped[date | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reversal_journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reversed_on: Mapped[date | None]
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Allocation(IdMixin, Base):
    """Part of a receipt or an issued credit note set against an invoice."""

    __tablename__ = "sales_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("(receipt_id IS NULL) <> (credit_note_id IS NULL)", name="one_source"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_documents.id"), index=True)
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("receipts.id"), index=True)
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_documents.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    allocated_on: Mapped[date]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VatRate(IdMixin, Base):
    __tablename__ = "vat_rates"
    __table_args__ = (CheckConstraint("rate >= 0 AND rate < 100", name="rate"),)

    effective_from: Mapped[date] = mapped_column(unique=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class WhtType(IdMixin, Base):
    __tablename__ = "wht_types"
    __table_args__ = (
        CheckConstraint(
            "(company_rate IS NULL OR company_rate BETWEEN 0 AND 100) AND "
            "(individual_rate IS NULL OR individual_rate BETWEEN 0 AND 100)",
            name="rates",
        ),
    )

    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    company_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    individual_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    position: Mapped[int] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class TaxReturn(IdMixin, Base):
    """A monthly return once someone starts it. Figures are frozen when it's filed."""

    __tablename__ = "tax_returns"
    __table_args__ = (
        UniqueConstraint("tax", "period_start"),
        CheckConstraint(_in("tax", TAXES), name="tax"),
        CheckConstraint(_in("status", RETURN_STATUSES), name="status"),
    )

    tax: Mapped[str] = mapped_column(String(5))
    period_start: Mapped[date]
    status: Mapped[str] = mapped_column(String(5), server_default="filed")
    output_amount: Mapped[Decimal | None] = mapped_column(Money)  # as filed
    input_amount: Mapped[Decimal | None] = mapped_column(Money)
    filed_on: Mapped[date | None]
    filing_reference: Mapped[str | None] = mapped_column(String(80))
    filed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    paid_on: Mapped[date | None]
    payment_reference: Mapped[str | None] = mapped_column(String(80))
    paid_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
