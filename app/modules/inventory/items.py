"""The inventory catalogue (ADR 0028): units of measure, item categories and their
accounts, items, warehouses and bins; and what the item pages show."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import numbering
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.inventory.models import (
    TRACKING,
    VALUATIONS,
    Bin,
    Item,
    ItemCategory,
    ItemValue,
    Lot,
    Serial,
    StockBalance,
    StockMove,
    Unit,
    Warehouse,
)

READ = "inventory.item.read"
MANAGE = "inventory.item.manage"
ZERO = Decimal(0)
VALUATION_LABELS = {"fifo": "FIFO", "average": "Weighted average"}
TRACKING_LABELS = {"none": "Not tracked", "lot": "Lot", "serial": "Serial"}


def can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor, permission):
        raise PermissionDenied()


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def parse_quantity(
    value: str, key: str, errors: dict[str, str], *, signed: bool = False
) -> Decimal:
    text = value.strip().replace(",", "")
    if not text:
        return ZERO
    try:
        amount = Decimal(text)
    except InvalidOperation:
        errors[key] = "Enter a quantity, like 12 or 2.5."
        return ZERO
    if (
        not amount.is_finite()
        or amount != amount.quantize(Decimal("0.001"))
        or (amount < 0 and not signed)
    ):
        errors[key] = "Enter a quantity, like 12 or 2.5."
        return ZERO
    return amount


def parse_cost(value: str, key: str, errors: dict[str, str]) -> Decimal | None:
    text = value.strip().replace(",", "").replace("₦", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        errors[key] = "Enter a unit cost, like 4,500.00."
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.0001")):
        errors[key] = "Enter a unit cost, like 4,500.00."
        return None
    return amount


def _audit(db: Session, actor: Actor, action: str, entity: str, entity_id: uuid.UUID) -> None:
    record_by(db, actor, action, entity_type=entity, entity_id=str(entity_id))


# ------------------------------------------------------------------ units and categories


def save_unit(
    db: Session, actor: Actor, unit_id: uuid.UUID | None, *, code: str, name: str, active: bool
) -> uuid.UUID:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    code = code.strip().upper()
    if not code or len(code) > 10:
        errors["unit_code"] = "Enter a short code, up to 10 letters."
    elif db.scalar(select(Unit.id).where(Unit.code == code, Unit.id != unit_id)):
        errors["unit_code"] = "There's already a unit with this code."
    if not name.strip():
        errors["unit_name"] = "Enter the unit's name."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(Unit, unit_id) if unit_id else None
    if row is None:
        row = Unit(code=code, name=name.strip()[:60])
        db.add(row)
    row.code, row.name, row.is_active = code, name.strip()[:60], active
    db.flush()
    _audit(db, actor, "inventory.unit.save", "inventory_unit", row.id)
    return row.id


def units(db: Session, *, active_only: bool = False) -> list[Unit]:
    query = select(Unit).order_by(Unit.code)
    if active_only:
        query = query.where(Unit.is_active)
    return list(db.scalars(query))


@dataclass(frozen=True, slots=True)
class CategoryInput:
    name: str
    inventory_account_id: str
    cogs_account_id: str
    adjustment_account_id: str
    expense_account_id: str
    is_active: bool = True


def _account(
    db: Session, value: str, key: str, types: tuple[str, ...], errors: dict[str, str]
) -> uuid.UUID | None:
    wanted = uuid_or_none(value)
    found = next((a for a in finance.postable(db) if a.id == wanted), None)
    if found is None or found.type not in types:
        errors[key] = "Choose an active account that takes postings."
        return None
    return found.id


def save_category(
    db: Session, actor: Actor, category_id: uuid.UUID | None, data: CategoryInput
) -> uuid.UUID:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    name = data.name.strip()
    if not name:
        errors["name"] = "Enter the category's name."
    elif db.scalar(
        select(ItemCategory.id).where(
            func.lower(ItemCategory.name) == name.lower(), ItemCategory.id != category_id
        )
    ):
        errors["name"] = "There's already a category with this name."
    stock = _account(db, data.inventory_account_id, "inventory_account_id", ("asset",), errors)
    cogs = _account(db, data.cogs_account_id, "cogs_account_id", ("expense",), errors)
    adjust = _account(
        db, data.adjustment_account_id, "adjustment_account_id", ("expense", "income"), errors
    )
    issue = _account(db, data.expense_account_id, "expense_account_id", ("expense",), errors)
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert stock and cogs and adjust and issue  # noqa: S101
    row = db.get(ItemCategory, category_id) if category_id else None
    if category_id and row is None:
        raise NotFound()
    if row is None:
        row = ItemCategory(
            name=name,
            inventory_account_id=stock,
            cogs_account_id=cogs,
            adjustment_account_id=adjust,
            expense_account_id=issue,
        )
        db.add(row)
    row.name, row.is_active = name[:80], data.is_active
    row.inventory_account_id, row.cogs_account_id = stock, cogs
    row.adjustment_account_id, row.expense_account_id = adjust, issue
    db.flush()
    _audit(db, actor, "inventory.category.save", "item_category", row.id)
    return row.id


def categories(db: Session, *, active_only: bool = False) -> list[ItemCategory]:
    query = select(ItemCategory).order_by(ItemCategory.name)
    if active_only:
        query = query.where(ItemCategory.is_active)
    return list(db.scalars(query))


# ------------------------------------------------------------------ warehouses and bins


def save_warehouse(
    db: Session,
    actor: Actor,
    warehouse_id: uuid.UUID | None,
    *,
    code: str,
    name: str,
    address: str,
    active: bool,
) -> uuid.UUID:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    code = code.strip().upper()
    if not code or len(code) > 10:
        errors["warehouse_code"] = "Enter a short code, up to 10 letters."
    elif db.scalar(
        select(Warehouse.id).where(Warehouse.code == code, Warehouse.id != warehouse_id)
    ):
        errors["warehouse_code"] = "There's already a warehouse with this code."
    if not name.strip():
        errors["warehouse_name"] = "Enter the warehouse's name."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(Warehouse, warehouse_id) if warehouse_id else None
    if row is None:
        row = Warehouse(code=code, name=name.strip()[:120])
        db.add(row)
    row.code, row.name = code, name.strip()[:120]
    row.address, row.is_active = address.strip() or None, active
    db.flush()
    _audit(db, actor, "inventory.warehouse.save", "warehouse", row.id)
    return row.id


def add_bin(db: Session, actor: Actor, warehouse_id: uuid.UUID, code: str) -> uuid.UUID:
    require(db, actor, MANAGE)
    code = code.strip().upper()
    if db.get(Warehouse, warehouse_id) is None:
        raise NotFound()
    if not code or len(code) > 20:
        raise ValidationFailed(field_errors={"bin_code": "Enter the bin's code."})
    if db.scalar(select(Bin.id).where(Bin.warehouse_id == warehouse_id, Bin.code == code)):
        raise ValidationFailed(field_errors={"bin_code": "That warehouse already has this bin."})
    row = Bin(warehouse_id=warehouse_id, code=code)
    db.add(row)
    db.flush()
    _audit(db, actor, "inventory.bin.add", "warehouse_bin", row.id)
    return row.id


def warehouses(db: Session, *, active_only: bool = False) -> list[Warehouse]:
    query = select(Warehouse).order_by(Warehouse.code)
    if active_only:
        query = query.where(Warehouse.is_active)
    return list(db.scalars(query))


def bins_by_warehouse(db: Session) -> dict[uuid.UUID, list[Bin]]:
    out: dict[uuid.UUID, list[Bin]] = {}
    for row in db.scalars(select(Bin).where(Bin.is_active).order_by(Bin.code)):
        out.setdefault(row.warehouse_id, []).append(row)
    return out


# ------------------------------------------------------------------ items


@dataclass(frozen=True, slots=True)
class ItemInput:
    code: str
    name: str
    category_id: str
    unit_id: str
    valuation: str
    tracking: str
    reorder_level: str = ""
    reorder_quantity: str = ""
    description: str = ""
    is_active: bool = True


def _moved(db: Session, item_id: uuid.UUID) -> bool:
    return bool(db.scalar(select(StockMove.id).where(StockMove.item_id == item_id).limit(1)))


def save_item(db: Session, actor: Actor, item_id: uuid.UUID | None, data: ItemInput) -> uuid.UUID:
    """Add or change an item. Its valuation and tracking can't change once stock has moved."""
    require(db, actor, MANAGE)
    row = db.get(Item, item_id) if item_id else None
    if item_id and row is None:
        raise NotFound()
    errors: dict[str, str] = {}
    code = data.code.strip().upper()
    if code and db.scalar(select(Item.id).where(Item.code == code, Item.id != item_id)):
        errors["code"] = "There's already an item with this code."
    if not data.name.strip():
        errors["name"] = "Enter the item's name."
    category = db.get(ItemCategory, uuid_or_none(data.category_id) or uuid.uuid4())
    if category is None or not category.is_active:
        errors["category_id"] = "Choose a category."
    unit = db.get(Unit, uuid_or_none(data.unit_id) or uuid.uuid4())
    if unit is None or not unit.is_active:
        errors["unit_id"] = "Choose a unit of measure."
    if data.valuation not in VALUATIONS:
        errors["valuation"] = "Choose FIFO or weighted average."
    if data.tracking not in TRACKING:
        errors["tracking"] = "Choose how it's tracked."
    if (
        row is not None
        and _moved(db, row.id)
        and (data.valuation != row.valuation or data.tracking != row.tracking)
    ):
        errors["valuation"] = "Stock has moved, so its valuation and tracking stay as they are."
    level = parse_quantity(data.reorder_level, "reorder_level", errors)
    again = parse_quantity(data.reorder_quantity, "reorder_quantity", errors)
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert category is not None and unit is not None  # noqa: S101
    if row is None:
        row = Item(
            code=code or numbering.next_number(db, "inventory_item"),
            name=data.name.strip(),
            category_id=category.id,
            unit_id=unit.id,
        )
        db.add(row)
    elif code:
        row.code = code
    row.name, row.description = data.name.strip()[:200], data.description.strip() or None
    row.category_id, row.unit_id = category.id, unit.id
    row.valuation, row.tracking = data.valuation, data.tracking
    row.reorder_level, row.reorder_quantity = level, again
    row.is_active = data.is_active
    db.flush()
    _audit(db, actor, "inventory.item.save", "item", row.id)
    return row.id


def item_choices(db: Session) -> list[Item]:
    return list(db.scalars(select(Item).where(Item.is_active).order_by(Item.code)))


@dataclass(frozen=True, slots=True)
class ItemRow:
    id: uuid.UUID
    code: str
    name: str
    category: str
    unit: str
    valuation: str
    tracking: str
    on_hand: Decimal
    value: Decimal
    reorder_level: Decimal
    low: bool
    is_active: bool


def item_rows(
    db: Session,
    actor: Actor,
    *,
    category: str = "",
    warehouse: str = "",
    view: str = "all",
    q: str = "",
) -> list[ItemRow]:
    require(db, actor, READ)
    held = select(
        ItemValue.item_id,
        func.sum(ItemValue.quantity).label("qty"),
        func.sum(ItemValue.value).label("value"),
    )
    if uuid_or_none(warehouse):
        held = held.where(ItemValue.warehouse_id == uuid_or_none(warehouse))
    totals = {r.item_id: (r.qty, r.value) for r in db.execute(held.group_by(ItemValue.item_id))}
    low = {i for i, _ in low_places(db)}
    query = select(Item).order_by(Item.code)
    if uuid_or_none(category):
        query = query.where(Item.category_id == uuid_or_none(category))
    if view == "inactive":
        query = query.where(~Item.is_active)
    elif view != "all_items":
        query = query.where(Item.is_active)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(func.lower(Item.name).like(like) | func.lower(Item.code).like(like))
    names = {c.id: c.name for c in categories(db)}
    codes = {u.id: u.code for u in units(db)}
    out = []
    for item in db.scalars(query.limit(1000)):
        qty, value = totals.get(item.id, (ZERO, ZERO))
        row = ItemRow(
            item.id,
            item.code,
            item.name,
            names.get(item.category_id, ""),
            codes.get(item.unit_id, ""),
            item.valuation,
            item.tracking,
            Decimal(qty or 0),
            Decimal(value or 0),
            item.reorder_level,
            item.id in low,
            item.is_active,
        )
        if view == "low" and not row.low:
            continue
        out.append(row)
    return out


def low_places(db: Session) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(item, warehouse) where an active item is at or below its reorder level."""
    rows = db.execute(
        select(ItemValue.item_id, ItemValue.warehouse_id)
        .join(Item, Item.id == ItemValue.item_id)
        .join(Warehouse, Warehouse.id == ItemValue.warehouse_id)
        .where(
            Item.is_active,
            Warehouse.is_active,
            Item.reorder_level > 0,
            ItemValue.quantity <= Item.reorder_level,
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


@dataclass(frozen=True, slots=True)
class Totals:
    value: Decimal
    items: int
    low: int
    warehouses: int


def totals(db: Session) -> Totals:
    value = Decimal(db.scalar(select(func.coalesce(func.sum(ItemValue.value), 0))) or 0)
    count = db.scalar(select(func.count()).select_from(Item).where(Item.is_active)) or 0
    places = db.scalar(select(func.count()).select_from(Warehouse).where(Warehouse.is_active))
    return Totals(value, count, len({i for i, _ in low_places(db)}), places or 0)


@dataclass(frozen=True, slots=True)
class Place:
    warehouse: str
    bin: str | None
    lot: str | None
    expiry: date | None
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class LedgerRow:
    move_date: date
    number: str
    kind: str
    warehouse: str
    quantity: Decimal
    value: Decimal
    balance: Decimal
    serials: str | None
    journal_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ItemView:
    item: Item
    category: ItemCategory
    unit: str
    on_hand: Decimal
    value: Decimal
    places: list[Place]
    values: list[tuple[str, Decimal, Decimal]]  # warehouse, quantity, value
    serials: list[tuple[str, str, str | None]]  # number, warehouse, bin
    ledger: list[LedgerRow]
    accounts: str


def item_view(db: Session, actor: Actor, item_id: uuid.UUID) -> ItemView:
    require(db, actor, READ)
    item = db.get(Item, item_id)
    if item is None:
        raise NotFound()
    category = db.get(ItemCategory, item.category_id)
    assert category is not None  # noqa: S101
    names = {w.id: w.name for w in warehouses(db)}
    bin_codes: dict[uuid.UUID | None, str] = dict(db.execute(select(Bin.id, Bin.code)).all())
    places = [
        Place(
            names.get(b.warehouse_id, ""),
            bin_codes.get(b.bin_id),
            lot.number if lot else None,
            lot.expiry if lot else None,
            b.quantity,
        )
        for b, lot in db.execute(
            select(StockBalance, Lot)
            .outerjoin(Lot, Lot.id == StockBalance.lot_id)
            .where(StockBalance.item_id == item.id, StockBalance.quantity > 0)
            .order_by(StockBalance.warehouse_id, Lot.expiry.asc().nulls_last())
        ).all()
    ]
    worth = list(
        db.scalars(select(ItemValue).where(ItemValue.item_id == item.id, ItemValue.quantity > 0))
    )
    serials = [
        (s.number, names.get(s.warehouse_id, "") if s.warehouse_id else "", bin_codes.get(s.bin_id))
        for s in db.scalars(
            select(Serial).where(Serial.item_id == item.id, Serial.in_stock).order_by(Serial.number)
        )
    ]
    moves = list(
        db.scalars(
            select(StockMove)
            .where(StockMove.item_id == item.id)
            .order_by(StockMove.move_date, StockMove.created_at, StockMove.id)
        )
    )
    running, ledger = ZERO, []
    for m in moves:
        running += m.quantity
        ledger.append(
            LedgerRow(
                m.move_date,
                m.number,
                m.kind,
                names.get(m.warehouse_id, ""),
                m.quantity,
                m.value,
                running,
                m.serials,
                m.journal_id,
            )
        )
    labels = {a.id: a.code for a in finance.all_accounts(db)}
    accounts = " / ".join(
        labels.get(i, "")
        for i in (
            category.inventory_account_id,
            category.cogs_account_id,
            category.adjustment_account_id,
            category.expense_account_id,
        )
    )
    unit = db.get(Unit, item.unit_id)
    return ItemView(
        item,
        category,
        unit.code if unit else "",
        sum((w.quantity for w in worth), ZERO),
        sum((w.value for w in worth), ZERO),
        places,
        [(names.get(w.warehouse_id, ""), w.quantity, w.value) for w in worth],
        serials,
        list(reversed(ledger))[:200],
        accounts,
    )
