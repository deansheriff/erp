"""Expenses (ADR 0020): categories and settings, claims and cash advances (one document
type), their lines and approval steps, advance repayments, and journals queued for the
ledger."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

KINDS = ("claim", "advance")
STATUSES = ("draft", "submitted", "approved", "rejected", "returned", "reimbursed", "cancelled")
STEP_KINDS = ("manager", "second", "finance")
STEP_STATUSES = ("waiting", "pending", "approved", "rejected", "returned")
METHODS = ("transfer", "cash")
JOURNAL_STATUSES = ("queued", "posted")

Money = Numeric(14, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ExpenseCategory(IdMixin, Base):
    __tablename__ = "expense_categories"
    __table_args__ = (
        CheckConstraint("claim_limit IS NULL OR claim_limit > 0", name="claim_limit"),
        CheckConstraint("receipt_above >= 0", name="receipt_above"),
    )

    name: Mapped[str] = mapped_column(String(80), unique=True)
    gl_account: Mapped[str] = mapped_column(String(20))  # a code until Phase 10
    claim_limit: Mapped[Decimal | None] = mapped_column(Money)
    receipt_above: Mapped[Decimal] = mapped_column(Money, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ExpenseSettings(IdMixin, Base):
    """One row."""

    __tablename__ = "expense_settings"
    __table_args__ = (
        CheckConstraint("second_level_above >= 0", name="second_level"),
        CheckConstraint("finance_above >= 0", name="finance"),
        CheckConstraint("weekly_limit IS NULL OR weekly_limit > 0", name="weekly"),
        CheckConstraint("monthly_limit IS NULL OR monthly_limit > 0", name="monthly"),
    )

    second_level_above: Mapped[Decimal] = mapped_column(Money)
    finance_above: Mapped[Decimal] = mapped_column(Money)
    weekly_limit: Mapped[Decimal | None] = mapped_column(Money)
    monthly_limit: Mapped[Decimal | None] = mapped_column(Money)
    advances_account: Mapped[str] = mapped_column(String(20))
    bank_account: Mapped[str] = mapped_column(String(20))


class PersonLimit(IdMixin, Base):
    __tablename__ = "expense_person_limits"
    __table_args__ = (
        CheckConstraint("weekly_limit IS NULL OR weekly_limit > 0", name="weekly"),
        CheckConstraint("monthly_limit IS NULL OR monthly_limit > 0", name="monthly"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), unique=True
    )
    weekly_limit: Mapped[Decimal | None] = mapped_column(Money)
    monthly_limit: Mapped[Decimal | None] = mapped_column(Money)


class ExpenseDocument(IdMixin, Base):
    """A claim (with lines) or a cash advance (an amount and a purpose)."""

    __tablename__ = "expense_documents"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="kind"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint(_in("payment_method", METHODS), name="payment_method"),
        CheckConstraint("total >= 0", name="total"),
        CheckConstraint("advance_applied >= 0", name="advance_applied"),
        CheckConstraint("kind = 'claim' OR advance_id IS NULL", name="advance_on_claim"),
        Index("ix_expense_documents_status", "status"),
    )

    number: Mapped[str] = mapped_column(String(30), unique=True)
    kind: Mapped[str] = mapped_column(String(8))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))  # the purpose, for an advance
    currency: Mapped[str] = mapped_column(String(3), server_default="NGN")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    status: Mapped[str] = mapped_column(String(10), server_default="draft")
    # The advance a claim is settled against, and how much of it was used at payment.
    advance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("expense_documents.id"))
    advance_applied: Mapped[Decimal] = mapped_column(Money, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]
    decided_at: Mapped[datetime | None]
    paid_at: Mapped[date | None]
    paid_amount: Mapped[Decimal | None] = mapped_column(Money)
    payment_method: Mapped[str | None] = mapped_column(String(10))
    payment_reference: Mapped[str | None] = mapped_column(String(80))
    paid_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    settled_at: Mapped[datetime | None]  # an advance with nothing left

    __authz__: ClassVar[AuthzColumns]


ExpenseDocument.__authz__ = AuthzColumns(owner_employee=ExpenseDocument.employee_id)


class ExpenseLine(IdMixin, Base):
    __tablename__ = "expense_lines"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("original_amount IS NULL OR original_amount > 0", name="original"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expense_documents.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date]
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("expense_categories.id"))
    description: Mapped[str] = mapped_column(String(200))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))  # ADR 0031
    amount: Mapped[Decimal] = mapped_column(Money)
    # Set when an approver changed the amount: what the employee claimed.
    original_amount: Mapped[Decimal | None] = mapped_column(Money)
    receipt_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    no_receipt_reason: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default="0")


class ExpenseStep(IdMixin, Base):
    """One approval in the chain of a submission. Only the first undecided step is
    `pending`; the ones after it are `waiting`."""

    __tablename__ = "expense_steps"
    __table_args__ = (
        CheckConstraint(_in("kind", STEP_KINDS), name="kind"),
        CheckConstraint(_in("status", STEP_STATUSES), name="status"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expense_documents.id", ondelete="CASCADE"), index=True
    )
    submission: Mapped[int] = mapped_column(SmallInteger)  # 1 for the first submission
    position: Mapped[int] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(String(8))
    # The person who should decide (None: organisation-wide approvers).
    approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(10), server_default="waiting")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)


class AdvanceRepayment(IdMixin, Base):
    __tablename__ = "expense_repayments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint(_in("method", METHODS), name="method"),
    )

    advance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expense_documents.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    received_on: Mapped[date]
    method: Mapped[str] = mapped_column(String(10))
    reference: Mapped[str | None] = mapped_column(String(80))
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExpenseJournal(IdMixin, Base):
    """A balanced journal waiting for the ledger (Phase 10)."""

    __tablename__ = "expense_journals"
    __table_args__ = (CheckConstraint(_in("status", JOURNAL_STATUSES), name="status"),)

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("expense_documents.id"), index=True)
    repayment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("expense_repayments.id"))
    journal_date: Mapped[date]
    memo: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(8), server_default="queued")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExpenseJournalLine(IdMixin, Base):
    __tablename__ = "expense_journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="one_side"),
    )

    journal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expense_journals.id", ondelete="CASCADE"), index=True
    )
    account: Mapped[str] = mapped_column(String(20))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))  # ADR 0031
    debit: Mapped[Decimal] = mapped_column(Money, server_default="0")
    credit: Mapped[Decimal] = mapped_column(Money, server_default="0")
