"""Stock movement documents (ADR 0028): issues to a cost centre and transfers, posted by
inventory staff; adjustments, posted by someone other than whoever recorded them. Posting
moves the stock and posts the journal (source `stock`)."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.inventory import stock
from app.modules.inventory.items import can, parse_cost, parse_quantity, require, uuid_or_none
from app.modules.inventory.models import (
    DOCUMENT_KINDS,
    Bin,
    Item,
    ItemCategory,
    StockDocument,
    StockDocumentLine,
    StockMove,
    Warehouse,
)
from app.org.models import CostCentre

MOVE = "inventory.stock.move"
APPROVE = "inventory.stock.approve"
ZERO = Decimal(0)
SEQUENCES = {"issue": "stock_issue", "transfer": "stock_transfer", "adjustment": "stock_adjustment"}
KIND_LABELS = {"issue": "Issue", "transfer": "Transfer", "adjustment": "Adjustment"}
STATUS_LABELS = {"draft": "Draft", "submitted": "Waiting for approval", "posted": "Posted"}
STATUS_TONES = {"draft": "neutral", "submitted": "warning", "posted": "success"}


def sod_blocked(db: Session, actor: Actor, created_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": created_by}, held
        )
    )


@dataclass(frozen=True, slots=True)
class LineInput:
    item_id: str
    quantity: str
    unit_cost: str = ""
    bin_id: str = ""
    to_bin_id: str = ""
    lot_number: str = ""
    expiry: date | None = None
    serials: str = ""


@dataclass(frozen=True, slots=True)
class DocumentInput:
    kind: str
    doc_date: date | None
    warehouse_id: str
    to_warehouse_id: str = ""
    cost_centre_id: str = ""
    account_id: str = ""
    reason: str = ""
    lines: tuple[LineInput, ...] = ()


def _active_warehouse(db: Session, value: str) -> Warehouse | None:
    row = db.get(Warehouse, uuid_or_none(value) or uuid.uuid4())
    return row if row is not None and row.is_active else None


def _check_header(db: Session, data: DocumentInput, errors: dict[str, str]) -> None:
    if data.kind not in DOCUMENT_KINDS:
        errors["form"] = "Choose an issue, transfer or adjustment."
    if data.doc_date is None:
        errors["doc_date"] = "Enter the date."
    elif data.doc_date > clock.today():
        errors["doc_date"] = "The date can't be in the future."
    if _active_warehouse(db, data.warehouse_id) is None:
        errors["warehouse_id"] = "Choose a warehouse."
    if data.kind == "transfer" and _active_warehouse(db, data.to_warehouse_id) is None:
        errors["to_warehouse_id"] = "Choose the warehouse it goes to."
    centre = uuid_or_none(data.cost_centre_id)
    if centre and db.get(CostCentre, centre) is None:
        errors["cost_centre_id"] = "Choose a cost centre."
    account = uuid_or_none(data.account_id)
    if account:
        wanted = ("expense",) if data.kind == "issue" else ("expense", "income", "equity")
        found = next((a for a in finance.postable(db) if a.id == account), None)
        if found is None or found.type not in wanted:
            errors["account_id"] = "Choose an account that takes postings."
    if data.kind == "adjustment" and not data.reason.strip():
        errors["reason"] = "Say why the stock is being adjusted."


def _check_line(
    db: Session, data: DocumentInput, line: LineInput, n: int, errors: dict[str, str]
) -> tuple[Item, Decimal, Decimal | None] | None:
    item = db.get(Item, uuid_or_none(line.item_id) or uuid.uuid4())
    if item is None or not item.is_active:
        errors[f"line_{n}"] = "Choose an item."
        return None
    qty = parse_quantity(line.quantity, f"line_{n}", errors, signed=data.kind == "adjustment")
    cost = parse_cost(line.unit_cost, f"line_{n}", errors)
    if f"line_{n}" in errors:
        return None
    if qty == 0 or (qty < 0 and data.kind != "adjustment"):
        errors[f"line_{n}"] = "Enter the quantity."
    elif data.kind == "adjustment" and qty > 0 and cost is None:
        errors[f"line_{n}"] = "Enter the unit cost of the stock being added."
    elif (
        data.kind == "adjustment"
        and qty > 0
        and item.tracking == "lot"
        and not line.lot_number.strip()
    ):
        errors[f"line_{n}"] = "Enter the lot number."
    elif item.tracking == "serial" and (
        qty != qty.to_integral_value() or len(stock.parse_serials(line.serials)) != abs(int(qty))
    ):
        errors[f"line_{n}"] = "Give one serial number for each unit."
    return item, qty, cost


def _check_bins(
    db: Session, data: DocumentInput, lines: list[LineInput], errors: dict[str, str]
) -> None:
    warehouse = uuid_or_none(data.warehouse_id)
    target = uuid_or_none(data.to_warehouse_id) if data.kind == "transfer" else None
    for n, line in enumerate(lines):
        for label, value, where in (
            ("bin", line.bin_id, warehouse),
            ("to bin", line.to_bin_id, target),
        ):
            found = db.get(Bin, uuid_or_none(value) or uuid.uuid4()) if value else None
            if value and (found is None or found.warehouse_id != where):
                errors[f"line_{n}"] = f"Choose a {label} in the right warehouse."
    if (
        data.kind == "transfer"
        and warehouse == target
        and any((line.bin_id or None) == (line.to_bin_id or None) for line in lines)
    ):
        errors["to_warehouse_id"] = "A transfer within a warehouse moves stock to another bin."


def save(
    db: Session, actor: Actor, document_id: uuid.UUID | None, data: DocumentInput
) -> uuid.UUID:
    """Create or change a draft."""
    require(db, actor, MOVE)
    row = _locked(db, document_id) if document_id else None
    if row is not None and row.status != "draft":
        raise Conflict("Only a draft can be changed.")
    errors: dict[str, str] = {}
    _check_header(db, data, errors)
    parsed = []
    for n, line in enumerate(data.lines):
        if not line.item_id and not line.quantity.strip():
            continue
        checked = _check_line(db, data, line, n, errors)
        if checked:
            parsed.append((line, *checked))
    warehouse = uuid_or_none(data.warehouse_id)
    target = uuid_or_none(data.to_warehouse_id) if data.kind == "transfer" else None
    _check_bins(db, data, [line for line, *_ in parsed], errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.doc_date is not None and warehouse is not None  # noqa: S101
    if row is None:
        row = StockDocument(
            kind=data.kind, doc_date=data.doc_date, warehouse_id=warehouse, created_by=actor.user_id
        )
        db.add(row)
        db.flush()
    row.doc_date, row.warehouse_id, row.to_warehouse_id = data.doc_date, warehouse, target
    row.cost_centre_id = uuid_or_none(data.cost_centre_id)
    row.account_id = uuid_or_none(data.account_id)
    row.reason = data.reason.strip() or None
    for old in db.scalars(select(StockDocumentLine).where(StockDocumentLine.document_id == row.id)):
        db.delete(old)
    db.flush()
    for position, (line, item, qty, cost) in enumerate(parsed, start=1):
        db.add(
            StockDocumentLine(
                document_id=row.id,
                position=position,
                item_id=item.id,
                quantity=qty,
                unit_cost=cost,
                bin_id=uuid_or_none(line.bin_id),
                to_bin_id=uuid_or_none(line.to_bin_id),
                lot_number=line.lot_number.strip()[:60] or None,
                expiry=line.expiry,
                serials=", ".join(stock.parse_serials(line.serials)) or None,
            )
        )
    db.flush()
    record_by(
        db,
        actor,
        f"inventory.{row.kind}.save",
        entity_type="stock_document",
        entity_id=str(row.id),
    )
    return row.id


def _locked(db: Session, document_id: uuid.UUID | None) -> StockDocument:
    row = db.scalars(
        select(StockDocument).where(StockDocument.id == document_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def lines_of(db: Session, document_id: uuid.UUID) -> list[StockDocumentLine]:
    return list(
        db.scalars(
            select(StockDocumentLine)
            .where(StockDocumentLine.document_id == document_id)
            .order_by(StockDocumentLine.position)
        )
    )


def delete(db: Session, actor: Actor, document_id: uuid.UUID) -> None:
    require(db, actor, MOVE)
    row = _locked(db, document_id)
    if row.status == "posted":
        raise Conflict("A posted movement can't be deleted.")
    record_by(
        db,
        actor,
        f"inventory.{row.kind}.delete",
        entity_type="stock_document",
        entity_id=str(row.id),
    )
    db.delete(row)
    db.flush()


def submit(db: Session, actor: Actor, document_id: uuid.UUID) -> None:
    """An adjustment waits for someone else to post it."""
    require(db, actor, MOVE)
    row = _locked(db, document_id)
    if row.kind != "adjustment" or row.status != "draft":
        raise Conflict("Only a draft adjustment is submitted for approval.")
    row.status, row.submitted_at = "submitted", clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "inventory.adjustment.submit",
        entity_type="stock_document",
        entity_id=str(row.id),
    )


def send_back(db: Session, actor: Actor, document_id: uuid.UUID) -> None:
    require(db, actor, APPROVE)
    row = _locked(db, document_id)
    if row.status != "submitted":
        raise Conflict("Only an adjustment waiting for approval can be sent back.")
    row.status, row.submitted_at = "draft", None
    db.flush()
    record_by(
        db,
        actor,
        "inventory.adjustment.send_back",
        entity_type="stock_document",
        entity_id=str(row.id),
    )


def post(db: Session, actor: Actor, document_id: uuid.UUID) -> str:
    """Post an issue or transfer (inventory staff), or an adjustment someone else recorded.
    Returns the number."""
    row = _locked(db, document_id)
    if row.kind == "adjustment":
        require(db, actor, APPROVE)
        if row.status != "submitted":
            raise Conflict("Submit the adjustment for approval first.")
        if sod_blocked(db, actor, row.created_by):
            raise PermissionDenied("You recorded this adjustment, so someone else posts it.")
    else:
        require(db, actor, MOVE)
        if row.status != "draft":
            raise Conflict("This has already been posted.")
    return _post(db, actor, row)


def _post(db: Session, actor: Actor, row: StockDocument) -> str:
    lines = lines_of(db, row.id)
    if not lines:
        raise Conflict("Add at least one line first.")
    row.number = numbering.next_number(db, SEQUENCES[row.kind])
    row.status, row.posted_by, row.posted_at = "posted", actor.user_id, clock.now()
    db.flush()
    src = stock.MoveSource("stock_document", row.id, row.number, row.doc_date, actor.user_id)
    moves: list[StockMove] = []
    journal: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = defaultdict(lambda: ZERO)
    categories = {c.id: c for c in db.scalars(select(ItemCategory))}
    for line in lines:
        item = db.get(Item, line.item_id)
        assert item is not None  # noqa: S101
        category = categories[item.category_id]
        value = _move_line(db, src, row, line=line, item=item, moves=moves)
        line.value = value
        if row.kind == "issue":
            expense = row.account_id or category.expense_account_id
            journal[(expense, row.cost_centre_id)] -= value  # value is negative: debit
            journal[(category.inventory_account_id, None)] += value
        elif row.kind == "adjustment":
            contra = row.account_id or category.adjustment_account_id
            journal[(category.inventory_account_id, None)] += value
            journal[(contra, None)] -= value
    row.value = sum((abs(line.value or ZERO) for line in lines), ZERO)
    posted = _post_journal(db, actor, row, journal)
    for move in moves:
        move.journal_id = posted
    row.journal_id = posted
    db.flush()
    record_by(
        db,
        actor,
        f"inventory.{row.kind}.post",
        entity_type="stock_document",
        entity_id=str(row.id),
        changes={"number": row.number, "value": str(row.value)},
    )
    return row.number


def _move_line(
    db: Session,
    src: stock.MoveSource,
    row: StockDocument,
    *,
    line: StockDocumentLine,
    item: Item,
    moves: list[StockMove],
) -> Decimal:
    """Move one line's stock. Returns its value, signed: in positive, out negative (a
    transfer nets to nothing)."""
    if row.kind == "adjustment" and line.quantity > 0:
        lot = (
            stock.lot_for_receipt(db, item, line.lot_number, line.expiry)
            if item.tracking == "lot"
            else None
        )
        serials = stock.serials_in(item, line.quantity, line.serials)
        value = stock.cents(line.quantity * (line.unit_cost or ZERO))
        stock.put_in(
            db,
            item,
            row.warehouse_id,
            bin_id=line.bin_id,
            lot_id=lot.id if lot else None,
            quantity=line.quantity,
            value=value,
            on=row.doc_date,
            serials=serials,
        )
        portion = stock.Portion(line.bin_id, lot.id if lot else None, line.quantity, serials)
        moves.append(
            stock.record(
                db,
                src,
                kind="adjustment_in",
                item_id=item.id,
                warehouse_id=row.warehouse_id,
                portion=portion,
                quantity=line.quantity,
                value=value,
            )
        )
        return value
    quantity = abs(line.quantity)
    portions = stock.allocate(
        db,
        item,
        row.warehouse_id,
        quantity=quantity,
        bin_id=line.bin_id,
        lot_number=line.lot_number,
        serials=line.serials,
    )
    values = stock.take_out(db, item, row.warehouse_id, portions)
    out_kind = {"issue": "issue", "transfer": "transfer_out", "adjustment": "adjustment_out"}
    for portion, value in zip(portions, values, strict=True):
        moves.append(
            stock.record(
                db,
                src,
                kind=out_kind[row.kind],
                item_id=item.id,
                warehouse_id=row.warehouse_id,
                portion=portion,
                quantity=-portion.quantity,
                value=-value,
            )
        )
        if row.kind == "transfer":
            assert row.to_warehouse_id is not None  # noqa: S101
            landed = stock.Portion(
                line.to_bin_id, portion.lot_id, portion.quantity, portion.serials
            )
            stock.put_in(
                db,
                item,
                row.to_warehouse_id,
                bin_id=line.to_bin_id,
                lot_id=portion.lot_id,
                quantity=portion.quantity,
                value=value,
                on=row.doc_date,
                serials=portion.serials,
            )
            moves.append(
                stock.record(
                    db,
                    src,
                    kind="transfer_in",
                    item_id=item.id,
                    warehouse_id=row.to_warehouse_id,
                    portion=landed,
                    quantity=portion.quantity,
                    value=value,
                )
            )
    total = sum(values, ZERO)
    return ZERO if row.kind == "transfer" else -total


def _post_journal(
    db: Session,
    actor: Actor,
    row: StockDocument,
    amounts: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal],
) -> uuid.UUID | None:
    """Post the journal (signed amounts: debit positive). None when nothing moved in value."""
    codes = {a.id: a.code for a in finance.all_accounts(db)}
    lines: list[finance.SystemLine] = [
        (codes[account], centre, amount, ZERO)
        if amount > 0
        else (codes[account], centre, ZERO, -amount)
        for (account, centre), amount in amounts.items()
        if amount != 0
    ]
    if len(lines) < 2:
        return None
    memo = f"{KIND_LABELS[row.kind]} {row.number}" + (f": {row.reason}" if row.reason else "")
    journal = finance.post_document_journal(
        db,
        source_id=row.id,
        journal_date=row.doc_date,
        memo=memo[:200],
        lines=lines,
        poster=actor.user_id,
        source="stock",
    )
    return journal.id


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: uuid.UUID
    kind: str
    number: str | None
    status: str
    doc_date: date
    warehouse: str
    to_warehouse: str | None
    detail: str | None
    lines: int
    value: Decimal
    created_by: str | None


def list_documents(db: Session, actor: Actor, tab: str) -> list[DocumentRow]:
    require(db, actor, "inventory.item.read")
    query = select(StockDocument).order_by(
        StockDocument.doc_date.desc(), StockDocument.created_at.desc()
    )
    if tab == "waiting":
        query = query.where(StockDocument.status != "posted")
    elif tab in DOCUMENT_KINDS:
        query = query.where(StockDocument.kind == tab)
    rows = list(db.scalars(query.limit(300)))
    names = {w.id: w.name for w in db.scalars(select(Warehouse))}
    centres = {c.id: c.name for c in db.scalars(select(CostCentre))}
    people = finance_user_names(db, {r.created_by for r in rows})
    counts: dict[uuid.UUID, int] = defaultdict(int)
    for line in db.scalars(
        select(StockDocumentLine).where(StockDocumentLine.document_id.in_([r.id for r in rows]))
    ):
        counts[line.document_id] += 1
    return [
        DocumentRow(
            r.id,
            r.kind,
            r.number,
            r.status,
            r.doc_date,
            names.get(r.warehouse_id, ""),
            names.get(r.to_warehouse_id) if r.to_warehouse_id else None,
            centres.get(r.cost_centre_id) if r.cost_centre_id else r.reason,
            counts[r.id],
            r.value,
            people.get(r.created_by) if r.created_by else None,
        )
        for r in rows
    ]


def finance_user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    from app.identity.models import User  # noqa: PLC0415  (kept local: a plain lookup)

    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


@dataclass(frozen=True, slots=True)
class DocumentView:
    doc: StockDocument
    lines: list[StockDocumentLine]
    items: dict[uuid.UUID, Item]
    created_by: str | None
    posted_by: str | None
    can_edit: bool
    can_post: bool
    post_blocked: str | None
    can_submit: bool
    can_send_back: bool


def get(db: Session, actor: Actor, document_id: uuid.UUID) -> DocumentView:
    require(db, actor, "inventory.item.read")
    row = db.get(StockDocument, document_id)
    if row is None:
        raise NotFound()
    lines = lines_of(db, row.id)
    items = {
        i.id: i for i in db.scalars(select(Item).where(Item.id.in_([x.item_id for x in lines])))
    }
    names = finance_user_names(db, {row.created_by, row.posted_by})
    mover = can(db, actor, MOVE)
    approver = can(db, actor, APPROVE)
    blocked = None
    allowed = False
    if row.kind == "adjustment":
        allowed = row.status == "submitted" and approver
        if allowed and sod_blocked(db, actor, row.created_by):
            allowed, blocked = False, "You recorded this, so someone else posts it."
    else:
        allowed = row.status == "draft" and mover
    return DocumentView(
        row,
        lines,
        items,
        names.get(row.created_by) if row.created_by else None,
        names.get(row.posted_by) if row.posted_by else None,
        row.status == "draft" and mover,
        allowed,
        blocked,
        row.kind == "adjustment" and row.status == "draft" and mover,
        row.status == "submitted" and approver,
    )


def posted_at_label(when: datetime | None) -> str:
    return f"{when:%d %b %Y}" if when else ""
