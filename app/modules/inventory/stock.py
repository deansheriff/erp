"""Stock on hand and its value (ADR 0028). Stock sits in a warehouse, optionally a bin
and, for lot items, a lot; it never goes negative. Valuation is per item and warehouse:
weighted average keeps a quantity and value, FIFO keeps cost layers by receipt (and lot).
Every change is a line in the stock ledger."""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, ValidationFailed
from app.modules.inventory.models import (
    Bin,
    CostLayer,
    Item,
    ItemValue,
    Lot,
    Serial,
    StockBalance,
    StockMove,
    Unit,
)

ZERO = Decimal(0)
CENT = Decimal("0.01")
QTY = Decimal("0.001")
UNIT_COST = Decimal("0.0001")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def per_unit(value: Decimal, quantity: Decimal) -> Decimal:
    return (value / quantity).quantize(UNIT_COST, rounding=ROUND_HALF_UP) if quantity else ZERO


def show(quantity: Decimal) -> str:
    return f"{quantity.normalize():,f}"


def parse_serials(text: str | None) -> list[str]:
    return [s for s in (p.strip() for p in re.split(r"[,\n;]+", text or "")) if s]


def unit_code(db: Session, item: Item) -> str:
    unit = db.get(Unit, item.unit_id)
    return unit.code if unit else ""


# ------------------------------------------------------------------ places


@dataclass(frozen=True, slots=True)
class Portion:
    """Some of an item at one place, leaving it."""

    bin_id: uuid.UUID | None
    lot_id: uuid.UUID | None
    quantity: Decimal
    serials: tuple[str, ...] = ()


def _balance(
    db: Session,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    bin_id: uuid.UUID | None,
    lot_id: uuid.UUID | None,
    *,
    create: bool,
) -> StockBalance | None:
    query = select(StockBalance).where(
        StockBalance.item_id == item_id,
        StockBalance.warehouse_id == warehouse_id,
        StockBalance.bin_id.is_(None) if bin_id is None else StockBalance.bin_id == bin_id,
        StockBalance.lot_id.is_(None) if lot_id is None else StockBalance.lot_id == lot_id,
    )
    row = db.scalars(query.with_for_update()).first()
    if row is None and create:
        row = StockBalance(
            item_id=item_id, warehouse_id=warehouse_id, bin_id=bin_id, lot_id=lot_id, quantity=ZERO
        )
        db.add(row)
        db.flush()
    return row


def on_hand(db: Session, item_id: uuid.UUID, warehouse_id: uuid.UUID | None = None) -> Decimal:
    query = select(func.coalesce(func.sum(StockBalance.quantity), 0)).where(
        StockBalance.item_id == item_id
    )
    if warehouse_id is not None:
        query = query.where(StockBalance.warehouse_id == warehouse_id)
    return Decimal(db.scalar(query) or 0)


def lot_named(db: Session, item_id: uuid.UUID, number: str) -> Lot | None:
    return db.scalars(select(Lot).where(Lot.item_id == item_id, Lot.number == number)).first()


def lot_for_receipt(db: Session, item: Item, number: str | None, expiry: date | None) -> Lot:
    """The lot stock comes into, created if new."""
    text = (number or "").strip()
    if not text:
        raise ValidationFailed(field_errors={"form": f"{item.code} needs a lot number."})
    lot = lot_named(db, item.id, text)
    if lot is None:
        lot = Lot(item_id=item.id, number=text[:60], expiry=expiry)
        db.add(lot)
        db.flush()
    elif expiry and lot.expiry and lot.expiry != expiry:
        raise Conflict(f"Lot {text} of {item.code} already expires on {lot.expiry:%d %b %Y}.")
    elif expiry and lot.expiry is None:
        lot.expiry = expiry
    return lot


def check_bin(db: Session, warehouse_id: uuid.UUID, bin_id: uuid.UUID | None) -> None:
    if bin_id is None:
        return
    row = db.get(Bin, bin_id)
    if row is None or row.warehouse_id != warehouse_id:
        raise ValidationFailed(field_errors={"form": "Choose a bin in that warehouse."})


def allocate(
    db: Session,
    item: Item,
    warehouse_id: uuid.UUID,
    *,
    quantity: Decimal,
    bin_id: uuid.UUID | None = None,
    lot_number: str | None = None,
    serials: str | None = None,
) -> list[Portion]:
    """Where stock leaves from: the given bin and lot, or for lot items the
    earliest-expiring lots first; serial items by their serial numbers. Refuses to go
    below zero."""
    unit = unit_code(db, item)
    if item.tracking == "serial":
        return _serial_portions(
            db, item, warehouse_id, quantity=quantity, bin_id=bin_id, serials=serials
        )
    query = (
        select(StockBalance, Lot)
        .outerjoin(Lot, Lot.id == StockBalance.lot_id)
        .outerjoin(Bin, Bin.id == StockBalance.bin_id)
        .where(
            StockBalance.item_id == item.id,
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.quantity > 0,
        )
    )
    if bin_id is not None:
        query = query.where(StockBalance.bin_id == bin_id)
    if lot_number and lot_number.strip():
        lot = lot_named(db, item.id, lot_number.strip())
        if lot is None:
            raise Conflict(f"{item.code} has no lot {lot_number.strip()}.")
        query = query.where(StockBalance.lot_id == lot.id)
    query = query.order_by(
        Lot.expiry.asc().nulls_last(), Lot.number, Bin.code.asc().nulls_first()
    ).with_for_update(of=StockBalance)
    portions: list[Portion] = []
    left = quantity
    for balance, _ in db.execute(query).all():
        if left <= 0:
            break
        take = min(balance.quantity, left)
        portions.append(Portion(balance.bin_id, balance.lot_id, take))
        left -= take
    if left > 0:
        have = quantity - left
        raise Conflict(
            f"Only {show(have)} {unit} of {item.code} {item.name} is on hand there; "
            f"{show(quantity)} is needed."
        )
    return portions


def _serial_portions(
    db: Session,
    item: Item,
    warehouse_id: uuid.UUID,
    *,
    quantity: Decimal,
    bin_id: uuid.UUID | None,
    serials: str | None,
) -> list[Portion]:
    numbers = parse_serials(serials)
    if quantity != quantity.to_integral_value() or len(numbers) != int(quantity):
        raise ValidationFailed(
            field_errors={"form": f"Give one serial number for each unit of {item.code}."}
        )
    if len(set(numbers)) != len(numbers):
        raise ValidationFailed(field_errors={"form": f"A serial number of {item.code} repeats."})
    by_bin: dict[uuid.UUID | None, list[str]] = {}
    for number in numbers:
        row = db.scalars(
            select(Serial).where(Serial.item_id == item.id, Serial.number == number)
        ).first()
        if row is None or not row.in_stock or row.warehouse_id != warehouse_id:
            raise Conflict(f"{item.code} serial {number} isn't in stock in that warehouse.")
        if bin_id is not None and row.bin_id != bin_id:
            raise Conflict(f"{item.code} serial {number} isn't in that bin.")
        by_bin.setdefault(row.bin_id, []).append(number)
    return [
        Portion(bin_, None, Decimal(len(found)), tuple(found)) for bin_, found in by_bin.items()
    ]


# ------------------------------------------------------------------ value


def _value_row(db: Session, item_id: uuid.UUID, warehouse_id: uuid.UUID) -> ItemValue:
    row = db.scalars(
        select(ItemValue)
        .where(ItemValue.item_id == item_id, ItemValue.warehouse_id == warehouse_id)
        .with_for_update()
    ).first()
    if row is None:
        row = ItemValue(item_id=item_id, warehouse_id=warehouse_id, quantity=ZERO, value=ZERO)
        db.add(row)
        db.flush()
    return row


def unit_cost_now(db: Session, item: Item, warehouse_id: uuid.UUID) -> Decimal | None:
    """What a unit is worth there now: the average, else the latest cost it came in at."""
    row = db.scalars(
        select(ItemValue).where(
            ItemValue.item_id == item.id, ItemValue.warehouse_id == warehouse_id
        )
    ).first()
    if row is not None and row.quantity > 0:
        return per_unit(row.value, row.quantity)
    last = db.execute(
        select(StockMove.value, StockMove.quantity)
        .where(StockMove.item_id == item.id, StockMove.quantity > 0)
        .order_by(StockMove.created_at.desc(), StockMove.id.desc())
        .limit(1)
    ).first()
    return per_unit(last[0], last[1]) if last else None


def _fifo_cost(
    db: Session, item: Item, warehouse_id: uuid.UUID, lot_id: uuid.UUID | None, quantity: Decimal
) -> Decimal:
    layers = db.scalars(
        select(CostLayer)
        .where(
            CostLayer.item_id == item.id,
            CostLayer.warehouse_id == warehouse_id,
            CostLayer.quantity_left > 0,
            CostLayer.lot_id.is_(None) if lot_id is None else CostLayer.lot_id == lot_id,
        )
        .order_by(CostLayer.received_on, CostLayer.id)
        .with_for_update()
    )
    cost, left = ZERO, quantity
    for layer in layers:
        if left <= 0:
            break
        take = min(layer.quantity_left, left)
        layer.quantity_left -= take
        cost += take * layer.unit_cost
        left -= take
    return cost


def take_out(
    db: Session, item: Item, warehouse_id: uuid.UUID, portions: list[Portion]
) -> list[Decimal]:
    """Remove the portions from stock. Returns each portion's value at cost."""
    total = sum((p.quantity for p in portions), ZERO)
    worth = _value_row(db, item.id, warehouse_id)
    values: list[Decimal] = []
    for portion in portions:
        balance = _balance(db, item.id, warehouse_id, portion.bin_id, portion.lot_id, create=False)
        if balance is None or balance.quantity < portion.quantity:
            raise Conflict(f"Not enough {item.code} {item.name} is on hand there.")
        balance.quantity -= portion.quantity
        if item.valuation == "fifo":
            values.append(_fifo_cost(db, item, warehouse_id, portion.lot_id, portion.quantity))
        elif worth.quantity:
            values.append(worth.value * portion.quantity / worth.quantity)
        else:
            values.append(ZERO)
        for number in portion.serials:
            row = db.scalars(
                select(Serial).where(Serial.item_id == item.id, Serial.number == number)
            ).one()
            row.in_stock, row.warehouse_id, row.bin_id = False, None, None
    rounded = [cents(v) for v in values]
    if total >= worth.quantity:  # the last of it takes whatever value is left
        drift = worth.value - sum(rounded, ZERO)
        rounded[-1] += drift
    taken = sum(rounded, ZERO)
    worth.quantity -= total
    worth.value = max(worth.value - taken, ZERO)
    if worth.quantity == 0:
        worth.value = ZERO
    db.flush()
    return rounded


def put_in(
    db: Session,
    item: Item,
    warehouse_id: uuid.UUID,
    *,
    bin_id: uuid.UUID | None,
    lot_id: uuid.UUID | None,
    quantity: Decimal,
    value: Decimal,
    on: date,
    serials: tuple[str, ...] = (),
) -> None:
    """Add stock at a value (quantity x unit cost, rounded to kobo)."""
    check_bin(db, warehouse_id, bin_id)
    balance = _balance(db, item.id, warehouse_id, bin_id, lot_id, create=True)
    assert balance is not None  # noqa: S101  (created)
    balance.quantity += quantity
    worth = _value_row(db, item.id, warehouse_id)
    worth.quantity += quantity
    worth.value += value
    if item.valuation == "fifo":
        db.add(
            CostLayer(
                item_id=item.id,
                warehouse_id=warehouse_id,
                lot_id=lot_id,
                received_on=on,
                quantity_left=quantity,
                unit_cost=per_unit(value, quantity),
            )
        )
    for number in serials:
        row = db.scalars(
            select(Serial).where(Serial.item_id == item.id, Serial.number == number)
        ).first()
        if row is None:
            row = Serial(item_id=item.id, number=number[:80])
            db.add(row)
        elif row.in_stock:
            raise Conflict(f"{item.code} serial {number} is already in stock.")
        row.in_stock, row.warehouse_id, row.bin_id = True, warehouse_id, bin_id
    db.flush()


def serials_in(item: Item, quantity: Decimal, text: str | None) -> tuple[str, ...]:
    """The serial numbers stock comes in with: one per unit for serial items."""
    if item.tracking != "serial":
        return ()
    numbers = parse_serials(text)
    if quantity != quantity.to_integral_value() or len(numbers) != int(quantity):
        raise ValidationFailed(
            field_errors={"form": f"Give one serial number for each unit of {item.code}."}
        )
    if len(set(numbers)) != len(numbers):
        raise ValidationFailed(field_errors={"form": f"A serial number of {item.code} repeats."})
    return tuple(numbers)


@dataclass(frozen=True, slots=True)
class MoveSource:
    source: str
    source_id: uuid.UUID
    number: str
    on: date
    created_by: uuid.UUID | None


def record(
    db: Session,
    src: MoveSource,
    *,
    kind: str,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    portion: Portion,
    quantity: Decimal,
    value: Decimal,
) -> StockMove:
    move = StockMove(
        move_date=src.on,
        kind=kind,
        item_id=item_id,
        warehouse_id=warehouse_id,
        bin_id=portion.bin_id,
        lot_id=portion.lot_id,
        quantity=quantity,
        value=value,
        serials=", ".join(portion.serials) or None,
        source=src.source,
        source_id=src.source_id,
        number=src.number,
        created_by=src.created_by,
    )
    db.add(move)
    return move
