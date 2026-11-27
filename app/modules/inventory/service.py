"""What other modules may use from inventory (ADR 0029): the stock link finance moves
stock through. `app/wiring.py` hands `LINK` to finance at start-up."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.modules.finance import service as finance
from app.modules.inventory import items, stock
from app.modules.inventory.models import Bin, Item, ItemCategory, StockMove, Unit, Warehouse

ZERO = Decimal(0)


def _stock_item(item: Item, category: ItemCategory, unit: str) -> finance.StockItem:
    return finance.StockItem(
        item.id,
        item.code,
        item.name,
        unit,
        item.tracking,
        item.is_active,
        category.inventory_account_id,
        category.cogs_account_id,
        category.adjustment_account_id,
    )


class InventoryLink:
    """Finance's view of inventory (finance.stock_link.StockLink)."""

    def items(self, db: Session) -> list[finance.StockItem]:
        rows = db.execute(
            select(Item, ItemCategory, Unit.code)
            .join(ItemCategory, ItemCategory.id == Item.category_id)
            .join(Unit, Unit.id == Item.unit_id)
            .where(Item.is_active)
            .order_by(Item.code)
        ).all()
        return [_stock_item(i, c, u) for i, c, u in rows]

    def item(self, db: Session, item_id: uuid.UUID) -> finance.StockItem | None:
        row = db.execute(
            select(Item, ItemCategory, Unit.code)
            .join(ItemCategory, ItemCategory.id == Item.category_id)
            .join(Unit, Unit.id == Item.unit_id)
            .where(Item.id == item_id)
        ).first()
        return _stock_item(row[0], row[1], row[2]) if row else None

    def warehouses(self, db: Session) -> list[tuple[uuid.UUID, str]]:
        return [(w.id, w.name) for w in items.warehouses(db, active_only=True)]

    def bins(self, db: Session) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
        return [
            (b.id, b.warehouse_id, b.code)
            for b in db.scalars(select(Bin).where(Bin.is_active).order_by(Bin.code))
        ]

    def _item(self, db: Session, line: finance.StockLine) -> Item:
        item = db.get(Item, line.item_id)
        warehouse = db.get(Warehouse, line.warehouse_id)
        if item is None:
            raise ValidationFailed(field_errors={line.key: "Choose a stock item."})
        if warehouse is None or not warehouse.is_active:
            raise ValidationFailed(field_errors={line.key: "Choose an active warehouse."})
        return item

    def move_in(
        self, db: Session, src: finance.StockSource, line: finance.StockLine, value: Decimal
    ) -> None:
        item = self._item(db, line)
        try:
            lot = (
                stock.lot_for_receipt(db, item, line.lot_number, line.expiry)
                if item.tracking == "lot"
                else None
            )
            serials = stock.serials_in(item, line.quantity, line.serials)
        except ValidationFailed as exc:
            message = next(iter((exc.field_errors or {}).values()), "Check the line.")
            raise ValidationFailed(field_errors={line.key: message}) from exc
        stock.put_in(
            db,
            item,
            line.warehouse_id,
            bin_id=line.bin_id,
            lot_id=lot.id if lot else None,
            quantity=line.quantity,
            value=value,
            on=src.on,
            serials=serials,
        )
        stock.record(
            db,
            stock.MoveSource(src.source, src.source_id, src.number, src.on, src.by),
            kind=src.kind,
            item_id=item.id,
            warehouse_id=line.warehouse_id,
            portion=stock.Portion(line.bin_id, lot.id if lot else None, line.quantity, serials),
            quantity=line.quantity,
            value=value,
        )
        db.flush()

    def move_out(self, db: Session, src: finance.StockSource, line: finance.StockLine) -> Decimal:
        item = self._item(db, line)
        try:
            portions = stock.allocate(
                db,
                item,
                line.warehouse_id,
                quantity=line.quantity,
                bin_id=line.bin_id,
                lot_number=line.lot_number,
                serials=line.serials,
            )
        except ValidationFailed as exc:
            message = next(iter((exc.field_errors or {}).values()), "Check the line.")
            raise ValidationFailed(field_errors={line.key: message}) from exc
        values = stock.take_out(db, item, line.warehouse_id, portions)
        place = stock.MoveSource(src.source, src.source_id, src.number, src.on, src.by)
        for portion, value in zip(portions, values, strict=True):
            stock.record(
                db,
                place,
                kind=src.kind,
                item_id=item.id,
                warehouse_id=line.warehouse_id,
                portion=portion,
                quantity=-portion.quantity,
                value=-value,
            )
        db.flush()
        return sum(values, ZERO)

    def taken_out(
        self, db: Session, source: str, source_id: uuid.UUID, item_id: uuid.UUID
    ) -> tuple[Decimal, Decimal]:
        quantity, value = db.execute(
            select(
                func.coalesce(func.sum(-StockMove.quantity), 0),
                func.coalesce(func.sum(-StockMove.value), 0),
            ).where(
                StockMove.source == source,
                StockMove.source_id == source_id,
                StockMove.item_id == item_id,
                StockMove.quantity < 0,
            )
        ).one()
        return Decimal(quantity), Decimal(value)

    def unit_cost(self, db: Session, item_id: uuid.UUID, warehouse_id: uuid.UUID) -> Decimal | None:
        item = db.get(Item, item_id)
        return stock.unit_cost_now(db, item, warehouse_id) if item else None

    def set_journal(
        self, db: Session, source: str, source_id: uuid.UUID, journal_id: uuid.UUID
    ) -> None:
        db.execute(
            update(StockMove)
            .where(StockMove.source == source, StockMove.source_id == source_id)
            .values(journal_id=journal_id)
        )


LINK = InventoryLink()
