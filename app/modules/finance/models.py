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
SOURCES = ("manual", "recurring", "reversal", "year_end", "payroll", "expenses")

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
