"""Procurement (ADR 0030): requisitions approved through tiers by amount, requests for
quotation to suppliers, their quotes and the award, and ratings of suppliers' orders."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

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
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

Money = Numeric(16, 2)
Quantity = Numeric(14, 3)
LEVELS = ("line_manager", "finance", "director")
REQUISITION_STATUSES = ("draft", "submitted", "approved", "rejected", "cancelled", "ordered")
STEP_STATUSES = ("pending", "approved", "rejected")
RFQ_STATUSES = ("draft", "sent", "awarded", "ordered", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ProcurementSettings(Base):
    """One row (id 1)."""

    __tablename__ = "procurement_settings"
    __table_args__ = (CheckConstraint("id = 1", name="one_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # Requisitions over this need three quotes (confirm).
    quote_threshold: Mapped[Decimal] = mapped_column(Money, server_default="1000000")


class ApprovalTier(IdMixin, Base):
    """A step added to a requisition's approval when its total is over `above`."""

    __tablename__ = "procurement_tiers"
    __table_args__ = (
        CheckConstraint(_in("level", LEVELS), name="level"),
        CheckConstraint("above >= 0", name="above"),
    )

    level: Mapped[str] = mapped_column(String(12), unique=True)
    above: Mapped[Decimal] = mapped_column(Money)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Requisition(IdMixin, Base):
    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        CheckConstraint(_in("status", REQUISITION_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    needed_by: Mapped[date | None]
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="draft")
    estimated_total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]

    __authz__: ClassVar[AuthzColumns]


Requisition.__authz__ = AuthzColumns(owner_employee=Requisition.employee_id)


class RequisitionLine(IdMixin, Base):
    """A stock item into a warehouse, or a description (with an account, if known)."""

    __tablename__ = "requisition_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND estimated_price >= 0", name="amounts"),
        CheckConstraint("(item_id IS NULL) = (warehouse_id IS NULL)", name="stock_place"),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("items.id"))
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    description: Mapped[str] = mapped_column(String(300))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finance_accounts.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    estimated_price: Mapped[Decimal] = mapped_column(Money)


class RequisitionStep(IdMixin, Base):
    """One approval a requisition needs, decided in `position` order."""

    __tablename__ = "requisition_steps"
    __table_args__ = (
        CheckConstraint(_in("level", LEVELS), name="level"),
        CheckConstraint(_in("status", STEP_STATUSES), name="status"),
        UniqueConstraint("requisition_id", "position"),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    level: Mapped[str] = mapped_column(String(12))
    # The line manager named at submission; None for the other levels.
    approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(8), server_default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)


class Rfq(IdMixin, Base):
    __tablename__ = "rfqs"
    __table_args__ = (CheckConstraint(_in("status", RFQ_STATUSES), name="status"),)

    number: Mapped[str] = mapped_column(String(20), unique=True)
    requisition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_requisitions.id"))
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    respond_by: Mapped[date | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
    award_reason: Mapped[str | None] = mapped_column(Text)
    awarded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    awarded_at: Mapped[datetime | None]
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("purchase_orders.id"))


# One live RFQ per requisition.
Index(
    "uq_rfqs_live_requisition",
    Rfq.requisition_id,
    unique=True,
    postgresql_where=Rfq.status != "cancelled",
)


class RfqSupplier(IdMixin, Base):
    __tablename__ = "rfq_suppliers"
    __table_args__ = (UniqueConstraint("rfq_id", "supplier_id"),)

    rfq_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rfqs.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    emailed_at: Mapped[datetime | None]
    declined: Mapped[bool] = mapped_column(Boolean, server_default=false())


class Quote(IdMixin, Base):
    """A supplier's reply to an RFQ, as recorded by procurement. Prices exclude VAT."""

    __tablename__ = "supplier_quotes"
    __table_args__ = (
        UniqueConstraint("rfq_id", "supplier_id"),
        CheckConstraint("delivery_days IS NULL OR delivery_days >= 0", name="delivery"),
    )

    rfq_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rfqs.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    reference: Mapped[str | None] = mapped_column(String(80))
    received_on: Mapped[date]
    valid_until: Mapped[date | None]
    delivery_days: Mapped[int | None] = mapped_column(SmallInteger)
    note: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    net: Mapped[Decimal] = mapped_column(Money, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Money, server_default="0")
    total: Mapped[Decimal] = mapped_column(Money, server_default="0")
    awarded: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class QuoteLine(IdMixin, Base):
    __tablename__ = "supplier_quote_lines"
    __table_args__ = (
        UniqueConstraint("quote_id", "line_id"),
        CheckConstraint("unit_price >= 0", name="price"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_quotes.id", ondelete="CASCADE"), index=True
    )
    line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requisition_lines.id", ondelete="CASCADE")
    )
    unit_price: Mapped[Decimal] = mapped_column(Money)
    vat_code: Mapped[str] = mapped_column(String(8))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="0")
    net: Mapped[Decimal] = mapped_column(Money, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Money, server_default="0")


class SupplierRating(IdMixin, Base):
    """Procurement's rating of one purchase order, once it's received or closed."""

    __tablename__ = "supplier_ratings"
    __table_args__ = (CheckConstraint("score BETWEEN 1 AND 5", name="score"),)

    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"), unique=True)
    score: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    rated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rated_at: Mapped[datetime] = mapped_column(server_default=func.now())
