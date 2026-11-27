"""Inventory (ADR 0028): units, categories and items; warehouses, bins, lots and serial
numbers; stock on hand and its valuation; movement documents, counts and material
requests; and the stock ledger."""

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

VALUATIONS = ("fifo", "average")
TRACKING = ("none", "lot", "serial")
DOCUMENT_KINDS = ("issue", "transfer", "adjustment")
DOCUMENT_STATUSES = ("draft", "submitted", "posted")
COUNT_STATUSES = ("counting", "submitted", "posted")
REQUEST_STATUSES = ("draft", "submitted", "approved", "rejected", "issued", "cancelled")
MOVE_KINDS = (
    "issue",
    "transfer_out",
    "transfer_in",
    "adjustment_in",
    "adjustment_out",
    "count_in",
    "count_out",
    "receipt",
    "purchase_return",
    "sale",
    "sale_return",
)

Money = Numeric(16, 2)
Quantity = Numeric(14, 3)
UnitCost = Numeric(16, 4)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# ------------------------------------------------------------------ catalogue


class Unit(IdMixin, Base):
    __tablename__ = "inventory_units"

    code: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ItemCategory(IdMixin, Base):
    __tablename__ = "item_categories"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    inventory_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    cogs_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    adjustment_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    expense_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finance_accounts.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Item(IdMixin, Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(_in("valuation", VALUATIONS), name="valuation"),
        CheckConstraint(_in("tracking", TRACKING), name="tracking"),
        CheckConstraint("reorder_level >= 0 AND reorder_quantity >= 0", name="reorder"),
    )

    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item_categories.id"), index=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_units.id"))
    valuation: Mapped[str] = mapped_column(String(7), server_default="average")
    tracking: Mapped[str] = mapped_column(String(6), server_default="none")
    reorder_level: Mapped[Decimal] = mapped_column(Quantity, server_default="0")
    reorder_quantity: Mapped[Decimal] = mapped_column(Quantity, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Warehouse(IdMixin, Base):
    __tablename__ = "warehouses"

    code: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Bin(IdMixin, Base):
    __tablename__ = "warehouse_bins"
    __table_args__ = (UniqueConstraint("warehouse_id", "code"),)

    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Lot(IdMixin, Base):
    __tablename__ = "stock_lots"
    __table_args__ = (UniqueConstraint("item_id", "number"),)

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"), index=True)
    number: Mapped[str] = mapped_column(String(60))
    expiry: Mapped[date | None]


class Serial(IdMixin, Base):
    """A serial-tracked unit and where it is, if in stock."""

    __tablename__ = "stock_serials"
    __table_args__ = (
        UniqueConstraint("item_id", "number"),
        CheckConstraint("in_stock = (warehouse_id IS NOT NULL)", name="placed"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"), index=True)
    number: Mapped[str] = mapped_column(String(80))
    in_stock: Mapped[bool] = mapped_column(Boolean, server_default="false")
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))


# ------------------------------------------------------------------ stock and its value


class StockBalance(IdMixin, Base):
    """How much of an item is in one place: a warehouse, optionally a bin and a lot."""

    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity"),
        Index(
            "uq_stock_balance_place",
            "item_id",
            "warehouse_id",
            "bin_id",
            "lot_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity, server_default="0")


class ItemValue(IdMixin, Base):
    """An item's quantity and value in one warehouse: what valuation works from."""

    __tablename__ = "item_values"
    __table_args__ = (
        UniqueConstraint("item_id", "warehouse_id"),
        CheckConstraint("quantity >= 0 AND value >= 0", name="amounts"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity, server_default="0")
    value: Mapped[Decimal] = mapped_column(Money, server_default="0")


class CostLayer(IdMixin, Base):
    """A FIFO layer: what is left of one receipt, at its unit cost."""

    __tablename__ = "cost_layers"
    __table_args__ = (CheckConstraint("quantity_left >= 0 AND unit_cost >= 0", name="amounts"),)

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id"))
    received_on: Mapped[date]
    quantity_left: Mapped[Decimal] = mapped_column(Quantity)
    unit_cost: Mapped[Decimal] = mapped_column(UnitCost)


class StockMove(IdMixin, Base):
    """The stock ledger: one line per item and place a document moved."""

    __tablename__ = "stock_moves"
    __table_args__ = (
        CheckConstraint(_in("kind", MOVE_KINDS), name="kind"),
        CheckConstraint("quantity <> 0", name="quantity"),
        Index("ix_stock_moves_item_date", "item_id", "move_date"),
    )

    move_date: Mapped[date]
    kind: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)  # in positive, out negative
    value: Mapped[Decimal] = mapped_column(Money)  # signed like the quantity
    serials: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))  # stock_document, stock_count
    source_id: Mapped[uuid.UUID]
    number: Mapped[str] = mapped_column(String(20))
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ------------------------------------------------------------------ documents


class StockDocument(IdMixin, Base):
    """An issue, transfer or adjustment. Posted ones are never edited."""

    __tablename__ = "stock_documents"
    __table_args__ = (
        CheckConstraint(_in("kind", DOCUMENT_KINDS), name="kind"),
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint("(status = 'posted') = (number IS NOT NULL)", name="numbered"),
    )

    kind: Mapped[str] = mapped_column(String(10), index=True)
    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    doc_date: Mapped[date]
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))  # ADR 0031
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finance_accounts.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_requests.id"))
    value: Mapped[Decimal] = mapped_column(Money, server_default="0")  # at posting
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


class StockDocumentLine(IdMixin, Base):
    __tablename__ = "stock_document_lines"
    __table_args__ = (CheckConstraint("quantity <> 0", name="quantity"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stock_documents.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)  # an adjustment's is signed
    unit_cost: Mapped[Decimal | None] = mapped_column(UnitCost)  # stock coming in
    bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))
    to_bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))
    lot_number: Mapped[str | None] = mapped_column(String(60))
    expiry: Mapped[date | None]
    serials: Mapped[str | None] = mapped_column(Text)
    request_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("material_request_lines.id")
    )
    value: Mapped[Decimal | None] = mapped_column(Money)  # at posting, signed


class StockCount(IdMixin, Base):
    __tablename__ = "stock_counts"
    __table_args__ = (CheckConstraint(_in("status", COUNT_STATUSES), name="status"),)

    number: Mapped[str] = mapped_column(String(20), unique=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    count_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(9), server_default="counting")
    note: Mapped[str | None] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Money, server_default="0")  # variance, at posting
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journals.id"))


class StockCountLine(IdMixin, Base):
    __tablename__ = "stock_count_lines"
    __table_args__ = (
        CheckConstraint("expected >= 0 AND (counted IS NULL OR counted >= 0)", name="amounts"),
    )

    count_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stock_counts.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"))
    bin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouse_bins.id"))
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id"))
    expected: Mapped[Decimal] = mapped_column(Quantity)
    counted: Mapped[Decimal | None] = mapped_column(Quantity)
    value: Mapped[Decimal | None] = mapped_column(Money)  # the variance's, at posting


# ------------------------------------------------------------------ material requests


class MaterialRequest(IdMixin, Base):
    __tablename__ = "material_requests"
    __table_args__ = (
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="numbered"),
    )

    number: Mapped[str | None] = mapped_column(String(20), unique=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    needed_by: Mapped[date | None]
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


MaterialRequest.__authz__ = AuthzColumns(owner_employee=MaterialRequest.employee_id)


class MaterialRequestLine(IdMixin, Base):
    __tablename__ = "material_request_lines"
    __table_args__ = (CheckConstraint("quantity > 0 AND issued >= 0", name="amounts"),)

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_requests.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    issued: Mapped[Decimal] = mapped_column(Quantity, server_default="0")


class LowStockAlert(IdMixin, Base):
    """An item and warehouse already listed in a day's low-stock digest."""

    __tablename__ = "low_stock_alerts"
    __table_args__ = (UniqueConstraint("alert_date", "item_id", "warehouse_id"),)

    alert_date: Mapped[date]
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
