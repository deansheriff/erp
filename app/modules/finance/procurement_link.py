"""What procurement uses from Payables (ADR 0030): suppliers, the accounts purchases are
charged to, drafting a purchase order from an awarded quote, and each supplier's order
history for performance. Exported through `finance/service.py`."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.identity.schemas import Actor
from app.modules.finance import periods, purchasing, sales
from app.modules.finance.models import (
    Account,
    Bill,
    BillLine,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseLine,
    PurchaseOrder,
    Supplier,
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class SupplierInfo:
    id: uuid.UUID
    code: str
    name: str
    email: str | None
    address: str | None
    is_active: bool


def supplier_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    """Active suppliers as (id, "code · name")."""
    return [
        (s.id, f"{s.code} · {s.name}")
        for s in db.scalars(select(Supplier).where(Supplier.is_active).order_by(Supplier.name))
    ]


def suppliers_by_id(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, SupplierInfo]:
    if not ids:
        return {}
    return {
        s.id: SupplierInfo(s.id, s.code, s.name, s.email, s.address, s.is_active)
        for s in db.scalars(select(Supplier).where(Supplier.id.in_(ids)))
    }


def all_suppliers(db: Session) -> list[SupplierInfo]:
    return [
        SupplierInfo(s.id, s.code, s.name, s.email, s.address, s.is_active)
        for s in db.scalars(select(Supplier).order_by(Supplier.name))
    ]


def purchase_accounts(db: Session) -> list[Account]:
    """What purchases are charged to: expense accounts and non-cash asset accounts."""
    return purchasing.cost_accounts(db)


@dataclass(frozen=True, slots=True)
class OrderLineDraft:
    description: str
    account_id: uuid.UUID | None  # None for a stock line: the item's inventory account
    quantity: Decimal
    unit_price: Decimal
    vat_code: str
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None


def draft_order(
    db: Session,
    actor: Actor,
    *,
    supplier_id: uuid.UUID,
    order_date: date,
    expected_date: date | None,
    reference: str,
    memo: str,
    cost_centre_id: uuid.UUID | None,
    lines: list[OrderLineDraft],
) -> uuid.UUID:
    """A draft purchase order, approved later by someone else as usual (ADR 0025)."""
    centre = str(cost_centre_id or "")
    data = purchasing.OrderInput(
        str(supplier_id),
        order_date,
        expected_date,
        reference,
        memo,
        [
            sales.LineInput(
                line.description,
                str(line.account_id or ""),
                centre,
                f"{line.quantity.normalize():f}",
                f"{line.unit_price:.2f}",
                line.vat_code,
                str(line.item_id or ""),
                str(line.warehouse_id or ""),
            )
            for line in lines
        ],
    )
    return purchasing.save_order(db, actor, None, data)


def order_status(db: Session, order_id: uuid.UUID) -> tuple[str, str]:
    """(number, shown status) of a purchase order: "Draft" until approved."""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return "", ""
    return order.number or "Draft", purchasing.shown_status(
        order, purchasing.progress(db, order.id)
    )


# ------------------------------------------------------------------ supplier history


@dataclass(frozen=True, slots=True)
class OrderFacts:
    """What happened on one of a supplier's purchase orders, for performance (ADR 0030)."""

    id: uuid.UUID
    number: str
    order_date: date
    expected_date: date | None
    status: str  # shown status
    total: Decimal
    lines: int
    lines_in_full: int  # received at least what was ordered
    ordered: Decimal
    received: Decimal  # before returns
    returned: Decimal  # stock sent back on debit notes
    receipts: list[date] = field(default_factory=list)
    bill_lines: int = 0  # on posted bills
    bill_lines_in_tolerance: int = 0

    @property
    def on_time(self) -> int:
        if self.expected_date is None:
            return 0
        return sum(1 for d in self.receipts if d <= self.expected_date)

    @property
    def late(self) -> int:
        if self.expected_date is None:
            return 0
        return sum(1 for d in self.receipts if d > self.expected_date)

    @property
    def days_late(self) -> int:
        if self.expected_date is None or not self.receipts:
            return 0
        return max((max(self.receipts) - self.expected_date).days, 0)


def _received(db: Session, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    return dict(
        db.execute(
            select(GoodsReceiptLine.line_id, func.sum(GoodsReceiptLine.quantity))
            .where(GoodsReceiptLine.line_id.in_(line_ids))
            .group_by(GoodsReceiptLine.line_id)
        ).all()
    )


def _returned(db: Session, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    rows = db.execute(
        select(BillLine.order_line_id, func.sum(BillLine.quantity))
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(
            BillLine.order_line_id.in_(line_ids),
            BillLine.item_id.is_not(None),
            Bill.kind == "debit_note",
            Bill.status == "posted",
        )
        .group_by(BillLine.order_line_id)
    ).all()
    return {line_id: qty for line_id, qty in rows if line_id}


def _bill_checks(
    db: Session, lines: dict[uuid.UUID, PurchaseLine], tolerance: Decimal
) -> tuple[int, int]:
    """(posted bill lines, those within the price tolerance of the PO)."""
    checked = within = 0
    for bill_line in db.scalars(
        select(BillLine)
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(
            BillLine.order_line_id.in_(list(lines)),
            Bill.kind == "bill",
            Bill.status == "posted",
        )
    ):
        po = lines[bill_line.order_line_id]  # type: ignore[index]
        checked += 1
        if po.unit_price:
            off = abs(bill_line.unit_price - po.unit_price) / po.unit_price * HUNDRED
            within += off <= tolerance
        else:
            within += bill_line.unit_price == 0
    return checked, within


def supplier_history(db: Session, supplier_id: uuid.UUID, *, since: date) -> list[OrderFacts]:
    """The supplier's approved purchase orders from `since`, newest first."""
    tolerance = periods.settings(db).price_tolerance
    orders = list(
        db.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.status.in_(("approved", "closed")),
                PurchaseOrder.order_date >= since,
            )
            .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.number.desc())
        )
    )
    out = []
    for order in orders:
        lines = {line.id: line for line in purchasing.order_lines(db, order.id)}
        ids = list(lines)
        received, returned = _received(db, ids), _returned(db, ids)
        receipts = sorted(
            db.scalars(select(GoodsReceipt.received_on).where(GoodsReceipt.order_id == order.id))
        )
        checked, within = _bill_checks(db, lines, tolerance)
        out.append(
            OrderFacts(
                order.id,
                order.number or "",
                order.order_date,
                order.expected_date,
                purchasing.shown_status(order, purchasing.progress(db, order.id)),
                order.total,
                len(lines),
                sum(1 for i, x in lines.items() if received.get(i, ZERO) >= x.quantity),
                sum((x.quantity for x in lines.values()), ZERO),
                sum(received.values(), ZERO),
                sum(returned.values(), ZERO),
                receipts,
                checked,
                within,
            )
        )
    return out


def orders_by_supplier(db: Session, *, since: date) -> dict[uuid.UUID, int]:
    """How many approved orders each supplier has had since the date."""
    out: dict[uuid.UUID, int] = defaultdict(int)
    for supplier_id, count in db.execute(
        select(PurchaseOrder.supplier_id, func.count())
        .where(
            PurchaseOrder.status.in_(("approved", "closed")),
            PurchaseOrder.order_date >= since,
        )
        .group_by(PurchaseOrder.supplier_id)
    ).all():
        out[supplier_id] = count
    return dict(out)
