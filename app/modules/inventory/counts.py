"""Stock counts (ADR 0028): a snapshot of what each place in a warehouse expects, what
was counted, and the variance (counted - expected) posted by someone else through each
category's adjustment account."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.inventory import stock
from app.modules.inventory.documents import (
    APPROVE,
    MOVE,
    finance_user_names,
    sod_blocked,
)
from app.modules.inventory.items import can, parse_quantity, require, uuid_or_none
from app.modules.inventory.models import (
    Bin,
    Item,
    ItemCategory,
    Lot,
    StockBalance,
    StockCount,
    StockCountLine,
    Warehouse,
)

ZERO = Decimal(0)
STATUS_LABELS = {"counting": "Counting", "submitted": "Waiting for approval", "posted": "Posted"}
STATUS_TONES = {"counting": "info", "submitted": "warning", "posted": "success"}


def _locked(db: Session, count_id: uuid.UUID) -> StockCount:
    row = db.scalars(select(StockCount).where(StockCount.id == count_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def start(db: Session, actor: Actor, *, warehouse_id: str, on: date | None, note: str) -> uuid.UUID:
    """Start a count: every place in the warehouse holding stock, with what it expects."""
    require(db, actor, MOVE)
    warehouse = db.get(Warehouse, uuid_or_none(warehouse_id) or uuid.uuid4())
    errors: dict[str, str] = {}
    if warehouse is None or not warehouse.is_active:
        errors["warehouse_id"] = "Choose a warehouse."
    if on is None:
        errors["on"] = "Enter the count date."
    elif on > clock.today():
        errors["on"] = "The date can't be in the future."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert warehouse is not None and on is not None  # noqa: S101
    row = StockCount(
        number=numbering.next_number(db, "stock_count"),
        warehouse_id=warehouse.id,
        count_date=on,
        note=note.strip() or None,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for balance in db.scalars(
        select(StockBalance)
        .join(Item, Item.id == StockBalance.item_id)
        .where(StockBalance.warehouse_id == warehouse.id, StockBalance.quantity > 0)
        .order_by(Item.code)
    ):
        db.add(
            StockCountLine(
                count_id=row.id,
                item_id=balance.item_id,
                bin_id=balance.bin_id,
                lot_id=balance.lot_id,
                expected=balance.quantity,
            )
        )
    db.flush()
    record_by(db, actor, "inventory.count.start", entity_type="stock_count", entity_id=str(row.id))
    return row.id


def add_line(
    db: Session, actor: Actor, count_id: uuid.UUID, *, item_id: str, bin_id: str, lot_number: str
) -> None:
    """Something found that the snapshot didn't expect."""
    require(db, actor, MOVE)
    row = _locked(db, count_id)
    if row.status != "counting":
        raise Conflict("Lines can only be added while counting.")
    item = db.get(Item, uuid_or_none(item_id) or uuid.uuid4())
    if item is None or not item.is_active:
        raise ValidationFailed(field_errors={"item_id": "Choose an item."})
    bin_ = uuid_or_none(bin_id)
    if bin_ and (db.get(Bin, bin_) is None or db.get(Bin, bin_).warehouse_id != row.warehouse_id):  # type: ignore[union-attr]
        raise ValidationFailed(field_errors={"item_id": "Choose a bin in this warehouse."})
    lot_id = None
    if item.tracking == "lot":
        lot = stock.lot_for_receipt(db, item, lot_number, None)
        lot_id = lot.id
    if db.scalar(
        select(StockCountLine.id).where(
            StockCountLine.count_id == row.id,
            StockCountLine.item_id == item.id,
            StockCountLine.bin_id.is_(None) if bin_ is None else StockCountLine.bin_id == bin_,
            StockCountLine.lot_id.is_(None) if lot_id is None else StockCountLine.lot_id == lot_id,
        )
    ):
        raise ValidationFailed(field_errors={"item_id": "That place is already on the sheet."})
    db.add(
        StockCountLine(count_id=row.id, item_id=item.id, bin_id=bin_, lot_id=lot_id, expected=ZERO)
    )
    db.flush()


def save_counts(db: Session, actor: Actor, count_id: uuid.UUID, counted: dict[str, str]) -> None:
    require(db, actor, MOVE)
    row = _locked(db, count_id)
    if row.status != "counting":
        raise Conflict("This count has been submitted.")
    errors: dict[str, str] = {}
    lines = {
        str(x.id): x
        for x in db.scalars(select(StockCountLine).where(StockCountLine.count_id == row.id))
    }
    for key, text in counted.items():
        line = lines.get(key)
        if line is None:
            continue
        if not text.strip():
            line.counted = None
            continue
        value = parse_quantity(text, f"c_{key}", errors)
        if f"c_{key}" not in errors:
            line.counted = value
    if errors:
        raise ValidationFailed(field_errors=errors)
    db.flush()


def submit(db: Session, actor: Actor, count_id: uuid.UUID) -> None:
    require(db, actor, MOVE)
    row = _locked(db, count_id)
    if row.status != "counting":
        raise Conflict("This count has already been submitted.")
    missing = db.scalar(
        select(StockCountLine.id)
        .where(StockCountLine.count_id == row.id, StockCountLine.counted.is_(None))
        .limit(1)
    )
    if missing:
        raise Conflict("Enter a count on every line first (0 if there's none).")
    row.status, row.submitted_at = "submitted", clock.now()
    db.flush()
    record_by(db, actor, "inventory.count.submit", entity_type="stock_count", entity_id=str(row.id))


def send_back(db: Session, actor: Actor, count_id: uuid.UUID) -> None:
    require(db, actor, APPROVE)
    row = _locked(db, count_id)
    if row.status != "submitted":
        raise Conflict("Only a count waiting for approval can be sent back.")
    row.status, row.submitted_at = "counting", None
    db.flush()


def delete(db: Session, actor: Actor, count_id: uuid.UUID) -> None:
    require(db, actor, MOVE)
    row = _locked(db, count_id)
    if row.status == "posted":
        raise Conflict("A posted count can't be deleted.")
    db.delete(row)
    db.flush()
    record_by(
        db, actor, "inventory.count.delete", entity_type="stock_count", entity_id=str(count_id)
    )


def post(db: Session, actor: Actor, count_id: uuid.UUID) -> None:
    """Post the variances: gains in at the current unit cost, losses out at cost."""
    require(db, actor, APPROVE)
    row = _locked(db, count_id)
    if row.status != "submitted":
        raise Conflict("Submit the count for approval first.")
    if sod_blocked(db, actor, row.created_by):
        raise PermissionDenied("You recorded this count, so someone else posts it.")
    src = stock.MoveSource("stock_count", row.id, row.number, row.count_date, actor.user_id)
    categories = {c.id: c for c in db.scalars(select(ItemCategory))}
    amounts: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)  # account: debit positive
    moves = []
    for line in db.scalars(select(StockCountLine).where(StockCountLine.count_id == row.id)):
        assert line.counted is not None  # noqa: S101  (checked on submit)
        diff = line.counted - line.expected
        if diff == 0:
            line.value = ZERO
            continue
        item = db.get(Item, line.item_id)
        assert item is not None  # noqa: S101
        if item.tracking == "serial":
            raise Conflict(
                f"{item.code} is serial-tracked: correct it with an adjustment giving the serial "
                "numbers, and count it as expected."
            )
        portion = stock.Portion(line.bin_id, line.lot_id, abs(diff))
        if diff > 0:
            unit_cost = stock.unit_cost_now(db, item, row.warehouse_id)
            if unit_cost is None:
                raise Conflict(f"{item.code} has no cost yet: add it with an adjustment instead.")
            value = stock.cents(diff * unit_cost)
            stock.put_in(
                db,
                item,
                row.warehouse_id,
                bin_id=line.bin_id,
                lot_id=line.lot_id,
                quantity=diff,
                value=value,
                on=row.count_date,
            )
            moves.append(
                stock.record(
                    db,
                    src,
                    kind="count_in",
                    item_id=item.id,
                    warehouse_id=row.warehouse_id,
                    portion=portion,
                    quantity=diff,
                    value=value,
                )
            )
        else:
            value = -stock.take_out(db, item, row.warehouse_id, [portion])[0]
            moves.append(
                stock.record(
                    db,
                    src,
                    kind="count_out",
                    item_id=item.id,
                    warehouse_id=row.warehouse_id,
                    portion=portion,
                    quantity=diff,
                    value=value,
                )
            )
        line.value = value
        category = categories[item.category_id]
        amounts[category.inventory_account_id] += value
        amounts[category.adjustment_account_id] -= value
    codes = {a.id: a.code for a in finance.all_accounts(db)}
    journal_lines: list[finance.SystemLine] = [
        (codes[a], None, v, ZERO) if v > 0 else (codes[a], None, ZERO, -v)
        for a, v in amounts.items()
        if v != 0
    ]
    journal_id = None
    if len(journal_lines) >= 2:
        journal = finance.post_document_journal(
            db,
            source_id=row.id,
            journal_date=row.count_date,
            memo=f"Stock count {row.number}",
            lines=journal_lines,
            poster=actor.user_id,
            source="stock",
        )
        journal_id = journal.id
    for move in moves:
        move.journal_id = journal_id
    row.value = sum((m.value for m in moves), ZERO)
    row.status, row.posted_by, row.posted_at, row.journal_id = (
        "posted",
        actor.user_id,
        clock.now(),
        journal_id,
    )
    db.flush()
    record_by(
        db,
        actor,
        "inventory.count.post",
        entity_type="stock_count",
        entity_id=str(row.id),
        changes={"variance": str(row.value)},
    )


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class CountLineView:
    id: uuid.UUID
    item_id: uuid.UUID
    code: str
    name: str
    tracking: str
    bin: str | None
    lot: str | None
    expected: Decimal
    counted: Decimal | None
    value: Decimal | None

    @property
    def variance(self) -> Decimal | None:
        return None if self.counted is None else self.counted - self.expected


@dataclass(frozen=True, slots=True)
class CountView:
    count: StockCount
    warehouse: str
    lines: list[CountLineView]
    counted: int
    created_by: str | None
    posted_by: str | None
    can_count: bool
    can_post: bool
    post_blocked: str | None
    can_send_back: bool


def get(db: Session, actor: Actor, count_id: uuid.UUID) -> CountView:
    require(db, actor, "inventory.item.read")
    row = db.get(StockCount, count_id)
    if row is None:
        raise NotFound()
    warehouse = db.get(Warehouse, row.warehouse_id)
    bins: dict[uuid.UUID | None, str] = dict(db.execute(select(Bin.id, Bin.code)).all())
    lines = []
    for line, item, lot in db.execute(
        select(StockCountLine, Item, Lot)
        .join(Item, Item.id == StockCountLine.item_id)
        .outerjoin(Lot, Lot.id == StockCountLine.lot_id)
        .where(StockCountLine.count_id == row.id)
        .order_by(Item.code, Lot.number)
    ).all():
        lines.append(
            CountLineView(
                line.id,
                item.id,
                item.code,
                item.name,
                item.tracking,
                bins.get(line.bin_id),
                lot.number if lot else None,
                line.expected,
                line.counted,
                line.value,
            )
        )
    names = finance_user_names(db, {row.created_by, row.posted_by})
    approver = can(db, actor, APPROVE)
    allowed = row.status == "submitted" and approver
    blocked = None
    if allowed and sod_blocked(db, actor, row.created_by):
        allowed, blocked = False, "You recorded this count, so someone else posts it."
    return CountView(
        row,
        warehouse.name if warehouse else "",
        lines,
        sum(1 for x in lines if x.counted is not None),
        names.get(row.created_by) if row.created_by else None,
        names.get(row.posted_by) if row.posted_by else None,
        row.status == "counting" and can(db, actor, MOVE),
        allowed,
        blocked,
        row.status == "submitted" and approver,
    )


@dataclass(frozen=True, slots=True)
class CountRow:
    id: uuid.UUID
    number: str
    warehouse: str
    count_date: date
    status: str
    value: Decimal
    created_by: str | None


def list_counts(db: Session, actor: Actor) -> list[CountRow]:
    require(db, actor, "inventory.item.read")
    rows = list(
        db.scalars(
            select(StockCount)
            .order_by(StockCount.count_date.desc(), StockCount.created_at.desc())
            .limit(200)
        )
    )
    names = {w.id: w.name for w in db.scalars(select(Warehouse))}
    people = finance_user_names(db, {r.created_by for r in rows})
    return [
        CountRow(
            r.id,
            r.number,
            names.get(r.warehouse_id, ""),
            r.count_date,
            r.status,
            r.value,
            people.get(r.created_by) if r.created_by else None,
        )
        for r in rows
    ]
