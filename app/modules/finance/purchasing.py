"""Purchase orders and goods received (ADR 0025). A PO is drafted by one person and
approved by another; goods are received against its lines, never more than is
outstanding. Receiving a stock line brings it into stock and posts Dr inventory, Cr goods
received not invoiced (ADR 0029); other lines post nothing."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import (
    accounts,
    journals,
    periods,
    project_link,
    sales,
    stock_link,
    tax,
)
from app.modules.finance.models import (
    Account,
    Bill,
    BillLine,
    GoodsReceipt,
    GoodsReceiptLine,
    Journal,
    PurchaseLine,
    PurchaseOrder,
    Supplier,
)
from app.org import service as org

READ = "finance.purchase.read"
CREATE = "finance.purchase.create"
APPROVE = "finance.purchase.approve"
CENT = Decimal("0.01")
ZERO = Decimal(0)
STATUS_LABELS = {
    "draft": "Draft",
    "approved": "Approved",
    "part_received": "Part received",
    "received": "Received",
    "closed": "Closed",
    "cancelled": "Cancelled",
}
STATUS_TONES = {
    "draft": "neutral",
    "approved": "info",
    "part_received": "warning",
    "received": "success",
    "closed": "neutral",
    "cancelled": "neutral",
}


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not _can(db, actor, permission):
        raise PermissionDenied()


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def sod_blocked(db: Session, actor: Actor, created_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": created_by}, held
        )
    )


def cost_accounts(db: Session) -> list[Account]:
    """What purchases are charged to: expense accounts, and asset accounts other than cash."""
    return [
        a
        for a in accounts.postable(db)
        if a.type == "expense" or (a.type == "asset" and a.cash_flow != "cash")
    ]


def price_line(line: PurchaseLine | BillLine, rate: Decimal | None) -> None:
    line.net = (line.quantity * line.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
    if line.vat_code == "standard":
        if rate is None:
            raise Conflict("No VAT rate is set for this date. Add one in Tax settings.")
        line.vat_rate, line.vat = rate, tax.vat_amount(line.net, rate)
    else:
        line.vat_rate, line.vat = ZERO, ZERO


# ------------------------------------------------------------------ orders


@dataclass(frozen=True, slots=True)
class OrderInput:
    supplier_id: str
    order_date: date | None
    expected_date: date | None
    reference: str
    memo: str
    lines: list[sales.LineInput]


def _locked(db: Session, order_id: uuid.UUID) -> PurchaseOrder:
    row = db.scalars(
        select(PurchaseOrder).where(PurchaseOrder.id == order_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def order_lines(db: Session, order_id: uuid.UUID) -> list[PurchaseLine]:
    return list(
        db.scalars(
            select(PurchaseLine)
            .where(PurchaseLine.order_id == order_id)
            .order_by(PurchaseLine.position)
        )
    )


def _price_order(db: Session, order: PurchaseOrder) -> None:
    rate = tax.vat_rate_on(db, order.order_date)
    net = vat = ZERO
    for line in order_lines(db, order.id):
        price_line(line, rate)
        net, vat = net + line.net, vat + line.vat
    order.net, order.vat, order.total = net, vat, net + vat
    db.flush()


def save_order(
    db: Session, actor: Actor, order_id: uuid.UUID | None, data: OrderInput
) -> uuid.UUID:
    _require(db, actor, CREATE)
    row = _locked(db, order_id) if order_id else None
    if row is not None and row.status != "draft":
        raise Conflict("Only a draft can be changed.")
    errors: dict[str, str] = {}
    supplier = db.get(Supplier, _uuid(data.supplier_id)) if _uuid(data.supplier_id) else None
    if supplier is None or not supplier.is_active:
        errors["supplier_id"] = "Choose an active supplier."
    if data.order_date is None:
        errors["order_date"] = "Enter the order date."
    if data.expected_date and data.order_date and data.expected_date < data.order_date:
        errors["expected_date"] = "Delivery can't be expected before the order date."
    parsed, line_errors = sales.parse_lines(
        db, data.lines, allowed=cost_accounts(db), stock_account=True
    )
    errors.update(line_errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert supplier is not None and data.order_date is not None  # noqa: S101  (checked above)
    if row is None:
        row = PurchaseOrder(status="draft", supplier_id=supplier.id, order_date=data.order_date)
        db.add(row)
    row.supplier_id, row.order_date, row.expected_date = (
        supplier.id,
        data.order_date,
        data.expected_date,
    )
    row.reference = data.reference.strip()[:80] or None
    row.memo = data.memo.strip()[:200] or None
    row.created_by = actor.user_id  # whoever last drafted it can't approve it
    db.flush()
    for old in order_lines(db, row.id):
        db.delete(old)
    db.flush()
    for position, line in enumerate(parsed, start=1):
        db.add(
            PurchaseLine(
                order_id=row.id,
                position=position,
                description=line.description,
                account_id=line.account.id,
                cost_centre_id=line.cost_centre_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                net=line.net,
                vat_code=line.vat_code,
                vat_rate=ZERO,
                vat=ZERO,
                item_id=line.item_id,
                warehouse_id=line.warehouse_id,
                project_id=line.project_id,
            )
        )
    db.flush()
    try:
        _price_order(db, row)
    except Conflict:  # no VAT rate yet: approving will say so
        db.flush()
    record_by(
        db,
        actor,
        "finance.purchase_order.draft",
        entity_type="purchase_order",
        entity_id=str(row.id),
        changes={"lines": {"from": None, "to": len(parsed)}},
    )
    return row.id


def delete_order(db: Session, actor: Actor, order_id: uuid.UUID) -> None:
    _require(db, actor, CREATE)
    row = _locked(db, order_id)
    if row.status != "draft":
        raise Conflict("Only a draft can be deleted.")
    record_by(
        db,
        actor,
        "finance.purchase_order.delete",
        entity_type="purchase_order",
        entity_id=str(row.id),
    )
    db.delete(row)
    db.flush()


def approve_order(db: Session, actor: Actor, order_id: uuid.UUID) -> str:
    _require(db, actor, APPROVE)
    row = _locked(db, order_id)
    if row.status != "draft":
        raise Conflict("This purchase order has already been approved.")
    if sod_blocked(db, actor, row.created_by):
        raise PermissionDenied("You drafted this purchase order, so someone else approves it.")
    supplier = db.get(Supplier, row.supplier_id)
    if supplier is None or not supplier.is_active:
        raise Conflict("The supplier is inactive. Reactivate them first.")
    _price_order(db, row)
    if not order_lines(db, row.id):
        raise Conflict("Add a line first.")
    row.number = numbering.next_number(db, "purchase_order")
    row.status, row.approved_by, row.approved_at = "approved", actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "finance.purchase_order.approve",
        entity_type="purchase_order",
        entity_id=str(row.id),
        changes={"number": {"from": None, "to": row.number}, "total": str(row.total)},
    )
    return row.number


def cancel_order(db: Session, actor: Actor, order_id: uuid.UUID) -> None:
    _require(db, actor, APPROVE)
    row = _locked(db, order_id)
    if row.status != "approved":
        raise Conflict("Only an approved purchase order can be cancelled.")
    if any(p.received for p in progress(db, row.id).values()):
        raise Conflict("Goods have been received against this order, so it can't be cancelled.")
    row.status = "cancelled"
    db.flush()
    record_by(
        db,
        actor,
        "finance.purchase_order.cancel",
        entity_type="purchase_order",
        entity_id=str(row.id),
        changes={"status": {"from": "approved", "to": "cancelled"}},
    )


# ------------------------------------------------------------------ progress and receiving


@dataclass(frozen=True, slots=True)
class Progress:
    ordered: Decimal
    received: Decimal
    billed: Decimal  # on posted bills

    @property
    def to_receive(self) -> Decimal:
        return max(self.ordered - self.received, ZERO)

    @property
    def to_bill(self) -> Decimal:
        return max(self.received - self.billed, ZERO)


def billed_quantities(
    db: Session, line_ids: list[uuid.UUID], *, exclude_bill: uuid.UUID | None = None
) -> dict[uuid.UUID, Decimal]:
    """Quantities on posted bills, less posted debit notes, by PO line."""
    if not line_ids:
        return {}
    query = (
        select(BillLine.order_line_id, Bill.kind, func.sum(BillLine.quantity))
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(BillLine.order_line_id.in_(line_ids), Bill.status == "posted")
        .group_by(BillLine.order_line_id, Bill.kind)
    )
    if exclude_bill:
        query = query.where(Bill.id != exclude_bill)
    out: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for line_id, kind, qty in db.execute(query).all():
        if line_id:
            out[line_id] += qty if kind == "bill" else -qty
    return dict(out)


def received_quantities(db: Session, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    """Quantities received, less stock sent back on posted debit notes (ADR 0029)."""
    if not line_ids:
        return {}
    out: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for line_id, qty in db.execute(
        select(GoodsReceiptLine.line_id, func.sum(GoodsReceiptLine.quantity))
        .where(GoodsReceiptLine.line_id.in_(line_ids))
        .group_by(GoodsReceiptLine.line_id)
    ).all():
        out[line_id] += qty
    for po_id, returned in db.execute(
        select(BillLine.order_line_id, func.sum(BillLine.quantity))
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(
            BillLine.order_line_id.in_(line_ids),
            BillLine.item_id.is_not(None),
            Bill.kind == "debit_note",
            Bill.status == "posted",
        )
        .group_by(BillLine.order_line_id)
    ).all():
        if po_id:
            out[po_id] -= returned
    return dict(out)


def progress(db: Session, order_id: uuid.UUID) -> dict[uuid.UUID, Progress]:
    lines = order_lines(db, order_id)
    ids = [line.id for line in lines]
    received = received_quantities(db, ids)
    billed = billed_quantities(db, ids)
    return {
        line.id: Progress(line.quantity, received.get(line.id, ZERO), billed.get(line.id, ZERO))
        for line in lines
    }


def shown_status(order: PurchaseOrder, done: dict[uuid.UUID, Progress]) -> str:
    if order.status != "approved":
        return order.status
    if done and all(p.received >= p.ordered for p in done.values()):
        return "received"
    if any(p.received for p in done.values()):
        return "part_received"
    return "approved"


@dataclass(frozen=True, slots=True)
class ReceiveDetail:
    """Where a stock line goes and what identifies it, as typed on the GRN form."""

    bin_id: str = ""
    lot_number: str = ""
    expiry: date | None = None
    serials: str = ""


def _receive_stock(
    db: Session,
    actor: Actor,
    grn: GoodsReceipt,
    taken: list[tuple[uuid.UUID, Decimal]],
    details: dict[str, ReceiveDetail],
) -> list[journals.SystemLine]:
    """Bring the GRN's stock lines into stock at the order price. Returns Dr inventory,
    Cr goods received not invoiced."""
    lines = {line.id: line for line in order_lines(db, grn.order_id)}
    stocked = [(lid, qty) for lid, qty in taken if lines[lid].item_id and lines[lid].warehouse_id]
    if not stocked:
        return []
    link = stock_link.link()
    src = stock_link.Source(
        "receipt", "goods_receipt", grn.id, grn.number, grn.received_on, actor.user_id
    )
    codes = {a.id: a.code for a in accounts.all_accounts(db)}
    errors: dict[str, str] = {}
    out: list[journals.SystemLine] = []
    total = ZERO
    for lid, qty in stocked:
        line = lines[lid]
        assert line.item_id is not None and line.warehouse_id is not None  # noqa: S101
        item = link.item(db, line.item_id)
        assert item is not None  # noqa: S101  (foreign key)
        detail = details.get(str(lid), ReceiveDetail())
        value = (qty * line.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        moving = stock_link.StockLine(
            f"qty_{lid}",
            line.item_id,
            line.warehouse_id,
            qty,
            bin_id=_uuid(detail.bin_id),
            lot_number=detail.lot_number.strip(),
            expiry=detail.expiry,
            serials=detail.serials,
        )
        try:
            link.move_in(db, src, moving, value)
        except ValidationFailed as exc:
            errors.update(exc.field_errors or {})
            continue
        if value:
            out.append((codes[item.inventory_account_id], line.cost_centre_id, value, ZERO))
            total += value
    if errors:
        raise ValidationFailed(field_errors=errors)
    if total:
        out.append((periods.settings(db).grni_code, None, ZERO, total))
    return out


def receive(
    db: Session,
    actor: Actor,
    order_id: uuid.UUID,
    *,
    received_on: date | None,
    delivery_note: str,
    note: str,
    quantities: dict[str, str],
    details: dict[str, ReceiveDetail] | None = None,
) -> str:
    """Record goods received against an approved order. Returns the GRN number."""
    _require(db, actor, CREATE)
    order = _locked(db, order_id)
    if order.status != "approved":
        raise Conflict("Goods can only be received against an approved purchase order.")
    done = progress(db, order.id)
    errors: dict[str, str] = {}
    if received_on is None:
        errors["received_on"] = "Enter the date the goods arrived."
    elif received_on < order.order_date:
        errors["received_on"] = "Goods can't arrive before the order date."
    taken = _quantities(done, quantities, errors)
    if not taken and not errors:
        errors["form"] = "Enter what was received on at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert received_on is not None  # noqa: S101  (checked above)
    grn = GoodsReceipt(
        number=numbering.next_number(db, "goods_receipt"),
        order_id=order.id,
        received_on=received_on,
        delivery_note=delivery_note.strip()[:80] or None,
        note=note.strip() or None,
        created_by=actor.user_id,
    )
    db.add(grn)
    db.flush()
    for lid, qty in taken:
        db.add(GoodsReceiptLine(receipt_id=grn.id, line_id=lid, quantity=qty))
    db.flush()
    stock_lines = _receive_stock(db, actor, grn, taken, details or {})
    if stock_lines:
        supplier = db.get(Supplier, order.supplier_id)
        journal = journals.post_document_journal(
            db,
            source_id=grn.id,
            journal_date=received_on,
            memo=f"Goods received {grn.number} · {supplier.name if supplier else ''}",
            lines=stock_lines,
            poster=actor.user_id,
            source="payable",
        )
        grn.journal_id = journal.id
        stock_link.link().set_journal(db, "goods_receipt", grn.id, journal.id)
        db.flush()
    record_by(
        db,
        actor,
        "finance.goods_receipt.record",
        entity_type="purchase_order",
        entity_id=str(order.id),
        changes={"grn": grn.number, "lines": len(taken)},
    )
    return grn.number


def _quantities(
    done: dict[uuid.UUID, Progress], quantities: dict[str, str], errors: dict[str, str]
) -> list[tuple[uuid.UUID, Decimal]]:
    """The GRN form's quantities, by PO line, never more than is still to come."""
    taken: list[tuple[uuid.UUID, Decimal]] = []
    for line_id, text in quantities.items():
        lid = _uuid(line_id)
        if lid is None or lid not in done or not text.strip():
            continue
        key = f"qty_{lid}"
        try:
            qty = Decimal(text.strip().replace(",", ""))
        except ArithmeticError:
            qty = Decimal(-1)
        if not qty.is_finite() or qty < 0 or qty != qty.quantize(Decimal("0.001")):
            errors[key] = "Enter a quantity."
        elif qty > done[lid].to_receive:
            errors[key] = f"Only {done[lid].to_receive.normalize():f} is still to come."
        elif qty:
            taken.append((lid, qty))
    return taken


def close_if_billed(db: Session, order_id: uuid.UUID) -> None:
    order = db.get(PurchaseOrder, order_id)
    done = progress(db, order_id)
    if (
        order
        and order.status == "approved"
        and done
        and all(p.billed >= p.ordered for p in done.values())
    ):
        order.status = "closed"
        db.flush()


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class OrderLineView:
    id: uuid.UUID
    description: str
    account_id: uuid.UUID
    account: str
    cost_centre_id: uuid.UUID | None
    cost_centre: str | None
    quantity: Decimal
    unit_price: Decimal
    net: Decimal
    vat_code: str
    vat_rate: Decimal
    vat: Decimal
    progress: Progress
    item_id: uuid.UUID | None = None  # a stock line (ADR 0029)
    item: str | None = None
    tracking: str = "none"
    warehouse_id: uuid.UUID | None = None
    warehouse: str | None = None
    project_id: uuid.UUID | None = None  # ADR 0031
    project: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiptView:
    id: uuid.UUID
    number: str
    received_on: date
    delivery_note: str | None
    note: str | None
    by: str | None
    lines: int
    journal: tuple[uuid.UUID, str] | None = None  # stock received (ADR 0029)


@dataclass(frozen=True, slots=True)
class OrderView:
    id: uuid.UUID
    number: str | None
    status: str
    shown_status: str
    supplier_id: uuid.UUID
    supplier: str
    order_date: date
    expected_date: date | None
    reference: str | None
    memo: str | None
    net: Decimal
    vat: Decimal
    total: Decimal
    created_by: str | None
    approved_by: str | None
    approved_at: datetime | None
    lines: list[OrderLineView] = field(default_factory=list)
    receipts: list[ReceiptView] = field(default_factory=list)
    bills: list[tuple[uuid.UUID, str | None, str]] = field(default_factory=list)
    can_edit: bool = False
    can_approve: bool = False
    approve_blocked: str | None = None
    can_receive: bool = False
    can_bill: bool = False
    can_cancel: bool = False


def get_order(db: Session, actor: Actor, order_id: uuid.UUID) -> OrderView:
    _require(db, actor, READ)
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise NotFound()
    supplier = db.get(Supplier, order.supplier_id)
    done = progress(db, order.id)
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    centres = dict(org.cost_centre_choices(db))
    grns = list(
        db.scalars(
            select(GoodsReceipt)
            .where(GoodsReceipt.order_id == order.id)
            .order_by(GoodsReceipt.received_on, GoodsReceipt.number)
        )
    )
    names = journals.user_names(
        db, {order.created_by, order.approved_by, *(g.created_by for g in grns)}
    )
    counts = dict(
        db.execute(
            select(GoodsReceiptLine.receipt_id, func.count())
            .where(GoodsReceiptLine.receipt_id.in_([g.id for g in grns]))
            .group_by(GoodsReceiptLine.receipt_id)
        ).all()
    )
    bills = [
        (b.id, b.number, b.status)
        for b in db.scalars(select(Bill).where(Bill.order_id == order.id).order_by(Bill.created_at))
    ]
    draft = order.status == "draft"
    can_create = _can(db, actor, CREATE)
    can_approve = draft and _can(db, actor, APPROVE)
    blocked = None
    if can_approve and sod_blocked(db, actor, order.created_by):
        can_approve, blocked = (
            False,
            "You drafted this purchase order, so someone else approves it.",
        )
    shown = shown_status(order, done)
    link = stock_link.link()
    items = {}
    for line in order_lines(db, order.id):
        if line.item_id and line.item_id not in items:
            items[line.item_id] = link.item(db, line.item_id)
    stores = dict(link.warehouses(db))
    projects = project_link.link().labels(
        db, {x.project_id for x in order_lines(db, order.id) if x.project_id}
    )
    posted = {
        j.id: j.number
        for j in db.scalars(
            select(Journal).where(Journal.id.in_([g.journal_id for g in grns if g.journal_id]))
        )
    }
    return OrderView(
        id=order.id,
        number=order.number,
        status=order.status,
        shown_status=shown,
        supplier_id=order.supplier_id,
        supplier=supplier.name if supplier else "",
        order_date=order.order_date,
        expected_date=order.expected_date,
        reference=order.reference,
        memo=order.memo,
        net=order.net,
        vat=order.vat,
        total=order.total,
        created_by=names.get(order.created_by) if order.created_by else None,
        approved_by=names.get(order.approved_by) if order.approved_by else None,
        approved_at=order.approved_at,
        lines=[
            OrderLineView(
                line.id,
                line.description,
                line.account_id,
                codes.get(line.account_id, ""),
                line.cost_centre_id,
                centres.get(line.cost_centre_id) if line.cost_centre_id else None,
                line.quantity,
                line.unit_price,
                line.net,
                line.vat_code,
                line.vat_rate,
                line.vat,
                done[line.id],
                line.item_id,
                _label(items.get(line.item_id)) if line.item_id else None,
                _tracking(items.get(line.item_id)) if line.item_id else "none",
                line.warehouse_id,
                stores.get(line.warehouse_id, "") if line.warehouse_id else None,
                line.project_id,
                projects.get(line.project_id) if line.project_id else None,
            )
            for line in order_lines(db, order.id)
        ],
        receipts=[
            ReceiptView(
                g.id,
                g.number,
                g.received_on,
                g.delivery_note,
                g.note,
                names.get(g.created_by) if g.created_by else None,
                counts.get(g.id, 0),
                (g.journal_id, posted[g.journal_id] or "")
                if g.journal_id and g.journal_id in posted
                else None,
            )
            for g in grns
        ],
        bills=bills,
        can_edit=draft and can_create,
        can_approve=can_approve,
        approve_blocked=blocked,
        can_receive=order.status == "approved" and can_create and shown != "received",
        can_bill=order.status == "approved"
        and can_create
        and any(p.to_bill for p in done.values()),
        can_cancel=order.status == "approved"
        and _can(db, actor, APPROVE)
        and not any(p.received for p in done.values()),
    )


def _label(item: stock_link.StockItem | None) -> str:
    return item.label if item else ""


def _tracking(item: stock_link.StockItem | None) -> str:
    return item.tracking if item else "none"


@dataclass(frozen=True, slots=True)
class OrderRow:
    id: uuid.UUID
    number: str | None
    supplier_id: uuid.UUID
    supplier: str
    order_date: date
    expected_date: date | None
    total: Decimal
    status: str  # shown


def list_orders(
    db: Session,
    actor: Actor,
    *,
    supplier_id: uuid.UUID | None,
    status: str,
    start: date | None,
    end: date | None,
) -> list[OrderRow]:
    _require(db, actor, READ)
    query = select(PurchaseOrder, Supplier.name).join(
        Supplier, Supplier.id == PurchaseOrder.supplier_id
    )
    if supplier_id:
        query = query.where(PurchaseOrder.supplier_id == supplier_id)
    if start:
        query = query.where(PurchaseOrder.order_date >= start)
    if end:
        query = query.where(PurchaseOrder.order_date <= end)
    rows = db.execute(
        query.order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.number.desc()).limit(500)
    ).all()
    out = []
    for order, name in rows:
        shown = (
            shown_status(order, progress(db, order.id))
            if order.status == "approved"
            else order.status
        )
        if status and shown != status:
            continue
        out.append(
            OrderRow(
                order.id,
                order.number,
                order.supplier_id,
                name,
                order.order_date,
                order.expected_date,
                order.total,
                shown,
            )
        )
    return out


def open_orders_for(db: Session, supplier_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    """(id, number, shown status) of the supplier's draft and approved orders."""
    out = []
    for order in db.scalars(
        select(PurchaseOrder)
        .where(
            PurchaseOrder.supplier_id == supplier_id,
            PurchaseOrder.status.in_(("draft", "approved")),
        )
        .order_by(PurchaseOrder.order_date.desc())
    ):
        out.append((order.id, order.number or "Draft", shown_status(order, progress(db, order.id))))
    return out
