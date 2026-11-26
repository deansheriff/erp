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
SOURCES = (
    "manual",
    "recurring",
    "reversal",
    "year_end",
    "payroll",
    "expenses",
    "receivable",
    "payable",
    "bank",
    "tax",
    "asset",
)

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
    # Payables (ADR 0025), to confirm.
    payables_code: Mapped[str] = mapped_column(String(20), server_default="2010")
    price_tolerance: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="2.00")
    # Banking (ADR 0026), to confirm.
    match_days: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    # Fixed assets (ADR 0027), to confirm.
    capitalisation_threshold: Mapped[Decimal] = mapped_column(Money, server_default="100000")
    revaluation_reserve_code: Mapped[str] = mapped_column(String(20), server_default="3300")
    disposal_code: Mapped[str] = mapped_column(String(20), server_default="4910")
    revaluation_loss_code: Mapped[str] = mapped_column(String(20), server_default="6910")


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
    paid_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"))
    payment_journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ------------------------------------------------------------------ payables (ADR 0025)

SUPPLIER_KINDS = ("company", "individual")
PO_STATUSES = ("draft", "approved", "closed", "cancelled")
BILL_KINDS = ("bill", "debit_note")
BILL_STATUSES = ("draft", "held", "posted")
PAYMENT_STATUSES = ("draft", "posted", "reversed")


class Supplier(IdMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint(_in("kind", SUPPLIER_KINDS), name="kind"),
        CheckConstraint("payment_terms_days BETWEEN 0 AND 365", name="terms"),
    )

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(10), server_default="company")
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(Text)
    tin: Mapped[str | None] = mapped_column(String(30))
    payment_terms_days: Mapped[int] = mapped_column(SmallInteger)
    wht_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wht_types.id"))
    bank_name: Mapped[str | None] = mapped_column(String(120))
    bank_code: Mapped[str | None] = mapped_column(String(10))
    account_number: Mapped[str | None] = mapped_column(String(10))
    account_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PurchaseOrder(IdMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint(_in("status", PO_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    order_date: Mapped[date]
    expected_date: Mapped[date | None]
    reference: Mapped[str | None] = mapped_column(String(80))
    memo: Mapped[str | None] = mapped_column(String(200))
    net: Mapped[Decimal] = mapped_column(Money, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Money, server_default="0")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]


class PurchaseLine(IdMixin, Base):
    __tablename__ = "purchase_lines"
    __table_args__ = (
        CheckConstraint(_in("vat_code", VAT_CODES), name="vat_code"),
        CheckConstraint("quantity > 0 AND unit_price >= 0", name="amounts"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True
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


class GoodsReceipt(IdMixin, Base):
    """Goods received against a purchase order. Nothing posts (ADR 0025)."""

    __tablename__ = "goods_receipts"

    number: Mapped[str] = mapped_column(String(20), unique=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    received_on: Mapped[date]
    delivery_note: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GoodsReceiptLine(IdMixin, Base):
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity"),)

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goods_receipts.id", ondelete="CASCADE"), index=True
    )
    line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_lines.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class Bill(IdMixin, Base):
    """A supplier invoice, or a debit note against one. Posted ones are never edited."""

    __tablename__ = "bills"
    __table_args__ = (
        CheckConstraint(_in("kind", BILL_KINDS), name="kind"),
        CheckConstraint(_in("status", BILL_STATUSES), name="status"),
        CheckConstraint("(status = 'posted') = (number IS NOT NULL)", name="numbered"),
        CheckConstraint("net >= 0 AND vat >= 0", name="amounts"),
    )

    kind: Mapped[str] = mapped_column(String(10), index=True)
    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(6), server_default="draft")
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    supplier_reference: Mapped[str | None] = mapped_column(String(80))  # their invoice number
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("purchase_orders.id"))
    bill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bills.id"))  # a debit note's
    bill_date: Mapped[date]
    due_date: Mapped[date | None]
    wht_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wht_types.id"))
    memo: Mapped[str | None] = mapped_column(String(200))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    override_reason: Mapped[str | None] = mapped_column(Text)
    net: Mapped[Decimal] = mapped_column(Money, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Money, server_default="0")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


# A supplier's invoice number is recorded once.
Index(
    "uq_bill_supplier_reference",
    Bill.supplier_id,
    func.lower(Bill.supplier_reference),
    unique=True,
    postgresql_where=(Bill.kind == "bill") & Bill.supplier_reference.is_not(None),
)


class BillLine(IdMixin, Base):
    __tablename__ = "bill_lines"
    __table_args__ = (
        CheckConstraint(_in("vat_code", VAT_CODES), name="vat_code"),
        CheckConstraint("quantity > 0 AND unit_price >= 0", name="amounts"),
    )

    bill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bills.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(String(300))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_lines.id"), index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    vat_code: Mapped[str] = mapped_column(String(8))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    vat: Mapped[Decimal] = mapped_column(Money)


class SupplierPayment(IdMixin, Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
        CheckConstraint("settled > 0 AND wht >= 0 AND cash = settled - wht", name="amounts"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(8), server_default="draft")
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    payment_date: Mapped[date]
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    reference: Mapped[str | None] = mapped_column(String(80))
    settled: Mapped[Decimal] = mapped_column(Money)
    wht: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reversal_journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reversed_on: Mapped[date | None]
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class SupplierPaymentLine(IdMixin, Base):
    """What a payment settles on one bill, and the WHT withheld from it."""

    __tablename__ = "supplier_payment_lines"
    __table_args__ = (
        UniqueConstraint("payment_id", "bill_id"),
        CheckConstraint("amount > 0 AND wht >= 0", name="amounts"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_payments.id", ondelete="CASCADE"), index=True
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bills.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)  # settled, including the WHT
    wht_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wht_types.id"))
    wht_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="0")
    wht: Mapped[Decimal] = mapped_column(Money, server_default="0")


# ------------------------------------------------------------------ banking (ADR 0026)

LINE_STATUSES = ("unmatched", "matched", "entry")
RULE_DIRECTIONS = ("in", "out", "any")
RECONCILIATION_STATUSES = ("draft", "submitted", "approved")
TRANSFER_STATUSES = ("draft", "posted")


class BankAccount(IdMixin, Base):
    """A bank account and its own ledger account, with the layout its CSV and Excel
    statements use."""

    __tablename__ = "bank_accounts"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"), unique=True)
    bank_name: Mapped[str | None] = mapped_column(String(120))
    bank_code: Mapped[str | None] = mapped_column(String(10))
    account_number: Mapped[str | None] = mapped_column(String(10))
    account_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    # Statement layout: a column header or a 1-based column number.
    csv_date: Mapped[str] = mapped_column(String(60), server_default="Date")
    csv_description: Mapped[str] = mapped_column(String(60), server_default="Description")
    csv_reference: Mapped[str | None] = mapped_column(String(60))
    csv_amount: Mapped[str | None] = mapped_column(String(60))  # signed, money in positive
    csv_money_in: Mapped[str | None] = mapped_column(String(60), server_default="Credit")
    csv_money_out: Mapped[str | None] = mapped_column(String(60), server_default="Debit")
    csv_date_format: Mapped[str] = mapped_column(String(20), server_default="%d/%m/%Y")
    csv_skip_rows: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BankStatement(IdMixin, Base):
    __tablename__ = "bank_statements"

    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    file_id: Mapped[uuid.UUID | None]  # the stored file (files service)
    filename: Mapped[str] = mapped_column(String(255))
    first_date: Mapped[date | None]
    last_date: Mapped[date | None]
    line_count: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    skipped: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    imported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    imported_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BankRule(IdMixin, Base):
    __tablename__ = "bank_rules"
    __table_args__ = (CheckConstraint(_in("direction", RULE_DIRECTIONS), name="direction"),)

    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"))
    contains: Mapped[str] = mapped_column(String(120))
    direction: Mapped[str] = mapped_column(String(3), server_default="any")
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    description: Mapped[str | None] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class BankReconciliation(IdMixin, Base):
    __tablename__ = "bank_reconciliations"
    __table_args__ = (
        UniqueConstraint("bank_account_id", "statement_date"),
        CheckConstraint(_in("status", RECONCILIATION_STATUSES), name="status"),
    )

    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    statement_date: Mapped[date]
    statement_balance: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    prepared_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StatementLine(IdMixin, Base):
    """A line of a bank statement: matched to a ledger entry, or given an entry that
    posts when its reconciliation is approved."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        UniqueConstraint("bank_account_id", "fingerprint"),
        CheckConstraint(_in("status", LINE_STATUSES), name="status"),
        CheckConstraint("amount <> 0", name="amount"),
        CheckConstraint("status <> 'matched' OR journal_line_id IS NOT NULL", name="matched"),
        CheckConstraint("(status = 'entry') = (entry_account_id IS NOT NULL)", name="entry"),
    )

    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bank_statements.id", ondelete="CASCADE"), index=True
    )
    line_date: Mapped[date] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(String(300))
    reference: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Money)  # money in positive, money out negative
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(9), server_default="unmatched")
    # The ledger line it matches; for an entry, the bank side of its journal once posted.
    journal_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_lines.id"), unique=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_rules.id", ondelete="SET NULL")
    )
    entry_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finance_accounts.id"))
    entry_cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    entry_description: Mapped[str | None] = mapped_column(String(200))
    entry_journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_reconciliations.id"), index=True
    )  # set when its reconciliation is approved: locked


class BankTransfer(IdMixin, Base):
    __tablename__ = "bank_transfers"
    __table_args__ = (
        CheckConstraint(_in("status", TRANSFER_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("from_account_id <> to_account_id", name="accounts"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(6), server_default="draft")
    from_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"))
    to_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    transfer_date: Mapped[date]
    reference: Mapped[str | None] = mapped_column(String(80))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


# ------------------------------------------------------------------ fixed assets (ADR 0027)

DEPRECIATION_METHODS = ("straight_line", "reducing_balance", "none")
ASSET_STATUSES = ("active", "disposed")
CHANGE_KINDS = ("revaluation", "disposal")
CHANGE_STATUSES = ("draft", "posted")
RUN_STATUSES = ("draft", "posted")

_METHOD_RULE = (
    "(method <> 'straight_line' OR life_months > 0) "
    "AND (method <> 'reducing_balance' OR (rate > 0 AND rate <= 100))"
)


class AssetCategory(IdMixin, Base):
    __tablename__ = "asset_categories"
    __table_args__ = (
        CheckConstraint(_in("method", DEPRECIATION_METHODS), name="method"),
        CheckConstraint(_METHOD_RULE, name="method_figures"),
        CheckConstraint(
            "method = 'none' OR (accumulated_account_id IS NOT NULL "
            "AND expense_account_id IS NOT NULL)",
            name="accounts",
        ),
    )

    name: Mapped[str] = mapped_column(String(80), unique=True)
    method: Mapped[str] = mapped_column(String(16))
    life_months: Mapped[int | None] = mapped_column(SmallInteger)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # % a year
    cost_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    accumulated_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finance_accounts.id")
    )
    expense_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finance_accounts.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class FixedAsset(IdMixin, Base):
    """An asset in the register. `gross` is its cost or valuation in the ledger, and
    `accumulated` the depreciation posted since; neither changes except by posting."""

    __tablename__ = "fixed_assets"
    __table_args__ = (
        CheckConstraint(_in("method", DEPRECIATION_METHODS), name="method"),
        CheckConstraint(_METHOD_RULE, name="method_figures"),
        CheckConstraint(_in("status", ASSET_STATUSES), name="status"),
        CheckConstraint(
            "cost > 0 AND gross >= 0 AND residual >= 0 AND accumulated >= 0 "
            "AND reserve >= 0 AND revaluation_losses >= 0",
            name="amounts",
        ),
        CheckConstraint("(status = 'disposed') = (disposed_on IS NOT NULL)", name="disposed"),
    )

    number: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    serial: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_categories.id"), index=True)
    method: Mapped[str] = mapped_column(String(16))
    life_months: Mapped[int | None] = mapped_column(SmallInteger)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    cost_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    accumulated_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finance_accounts.id")
    )
    expense_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finance_accounts.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    acquired_on: Mapped[date]
    cost: Mapped[Decimal] = mapped_column(Money)  # what it cost
    gross: Mapped[Decimal] = mapped_column(Money)  # cost, or its latest valuation
    residual: Mapped[Decimal] = mapped_column(Money, server_default="0")
    accumulated: Mapped[Decimal] = mapped_column(Money, server_default="0")
    months_left: Mapped[int | None] = mapped_column(SmallInteger)  # straight line
    depreciated_to: Mapped[date | None]  # first day of the last month charged
    reserve: Mapped[Decimal] = mapped_column(Money, server_default="0")
    revaluation_losses: Mapped[Decimal] = mapped_column(Money, server_default="0")
    status: Mapped[str] = mapped_column(String(8), server_default="active")
    disposed_on: Mapped[date | None]
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    bill_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bill_lines.id"), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssetChange(IdMixin, Base):
    """A revaluation or disposal: recorded by one person, approved by another, which
    posts its journal."""

    __tablename__ = "asset_changes"
    __table_args__ = (
        CheckConstraint(_in("kind", CHANGE_KINDS), name="kind"),
        CheckConstraint(_in("status", CHANGE_STATUSES), name="status"),
        CheckConstraint("amount >= 0", name="amount"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixed_assets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(11))
    status: Mapped[str] = mapped_column(String(6), server_default="draft")
    change_date: Mapped[date]
    amount: Mapped[Decimal] = mapped_column(Money)  # the new value, or the proceeds
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"))
    reason: Mapped[str] = mapped_column(Text)
    carrying: Mapped[Decimal | None] = mapped_column(Money)  # at approval
    result: Mapped[Decimal | None] = mapped_column(Money)  # gain (+) or loss (-), at approval
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


# An asset has at most one change waiting.
Index(
    "uq_asset_change_waiting",
    AssetChange.asset_id,
    unique=True,
    postgresql_where=AssetChange.status == "draft",
)


class DepreciationRun(IdMixin, Base):
    __tablename__ = "depreciation_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month"),
    )

    month: Mapped[date] = mapped_column(unique=True)  # its first day
    status: Mapped[str] = mapped_column(String(6), server_default="draft")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    prepared_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))  # None: task
    prepared_at: Mapped[datetime] = mapped_column(server_default=func.now())
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


class DepreciationLine(IdMixin, Base):
    __tablename__ = "depreciation_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "asset_id"),
        CheckConstraint("months > 0 AND amount > 0", name="amounts"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("depreciation_runs.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixed_assets.id"), index=True)
    months: Mapped[int] = mapped_column(SmallInteger)
    amount: Mapped[Decimal] = mapped_column(Money)
