"""How finance moves stock without importing inventory (ADR 0029). Inventory implements
`StockLink`; `app/wiring.py` connects it with `set_link` at start-up."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class StockItem:
    id: uuid.UUID
    code: str
    name: str
    unit: str
    tracking: str  # none, lot, serial
    is_active: bool
    inventory_account_id: uuid.UUID
    cogs_account_id: uuid.UUID
    adjustment_account_id: uuid.UUID

    @property
    def label(self) -> str:
        return f"{self.code} {self.name} · {self.unit}"


@dataclass(frozen=True, slots=True)
class StockLine:
    """One document line's stock, coming in or going out. `key` names the form field an
    error belongs to."""

    key: str
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal
    bin_id: uuid.UUID | None = None
    lot_number: str = ""
    expiry: date | None = None
    serials: str = ""


@dataclass(frozen=True, slots=True)
class Source:
    """The finance document moving the stock."""

    kind: str  # receipt, purchase_return, sale, sale_return
    source: str  # goods_receipt, bill, sales_document
    source_id: uuid.UUID
    number: str
    on: date
    by: uuid.UUID | None


class StockLink(Protocol):
    def items(self, db: Session) -> list[StockItem]: ...

    def item(self, db: Session, item_id: uuid.UUID) -> StockItem | None: ...

    def warehouses(self, db: Session) -> list[tuple[uuid.UUID, str]]: ...

    def bins(self, db: Session) -> list[tuple[uuid.UUID, uuid.UUID, str]]: ...

    def move_in(self, db: Session, src: Source, line: StockLine, value: Decimal) -> None: ...

    def move_out(self, db: Session, src: Source, line: StockLine) -> Decimal: ...

    def taken_out(
        self, db: Session, source: str, source_id: uuid.UUID, item_id: uuid.UUID
    ) -> tuple[Decimal, Decimal]: ...

    def unit_cost(
        self, db: Session, item_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> Decimal | None: ...

    def set_journal(
        self, db: Session, source: str, source_id: uuid.UUID, journal_id: uuid.UUID
    ) -> None: ...


_link: StockLink | None = None


def set_link(link: StockLink) -> None:
    global _link  # noqa: PLW0603  (set once at start-up by app/wiring.py)
    _link = link


def link() -> StockLink:
    if _link is None:
        raise RuntimeError("Inventory isn't connected: call app.wiring.configure_runtime().")
    return _link
