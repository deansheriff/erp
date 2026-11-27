"""Material requests (ADR 0028): any employee asks for items for their own work; their
line manager decides (or someone allowed to decide any, when there's no manager); then
inventory staff issue it, in parts if need be, as issue documents charged to the
requester's cost centre."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.authz import service as authz
from app.authz.engine import Resource
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.inventory import documents, stock
from app.modules.inventory.items import can, parse_quantity, uuid_or_none
from app.modules.inventory.models import (
    Item,
    MaterialRequest,
    MaterialRequestLine,
    StockDocument,
    StockDocumentLine,
    Unit,
    Warehouse,
)
from app.org import service as org

CREATE = "inventory.request.create"
READ = "inventory.request.read"
APPROVE = "inventory.request.approve"
APPROVE_ANY = "inventory.request.approve_any"
ZERO = Decimal(0)
STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Waiting for approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "issued": "Issued",
    "cancelled": "Cancelled",
    "partly": "Partly issued",
}
STATUS_TONES = {
    "draft": "neutral",
    "submitted": "warning",
    "approved": "info",
    "rejected": "danger",
    "issued": "success",
    "cancelled": "neutral",
    "partly": "info",
}


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def _row(db: Session, request_id: uuid.UUID, *, lock: bool = False) -> MaterialRequest:
    query = select(MaterialRequest).where(MaterialRequest.id == request_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def _lines(db: Session, request_id: uuid.UUID) -> list[MaterialRequestLine]:
    return list(
        db.scalars(
            select(MaterialRequestLine)
            .where(MaterialRequestLine.request_id == request_id)
            .order_by(MaterialRequestLine.position)
        )
    )


def _mine(db: Session, actor: Actor) -> uuid.UUID:
    employee = people.employee_of_user(db, actor.user_id)
    if employee is None or not _can(db, actor, CREATE, employee):
        raise PermissionDenied("Material requests are for people on the staff list.")
    return employee


@dataclass(frozen=True, slots=True)
class RequestInput:
    warehouse_id: str
    needed_by: date | None
    purpose: str
    lines: tuple[tuple[str, str], ...]  # (item id, quantity)


def _parse_lines(
    db: Session, lines: tuple[tuple[str, str], ...], errors: dict[str, str]
) -> list[tuple[uuid.UUID, Decimal]]:
    parsed: list[tuple[uuid.UUID, Decimal]] = []
    for n, (item_id, quantity) in enumerate(lines):
        if not item_id and not quantity.strip():
            continue
        item = db.get(Item, uuid_or_none(item_id) or uuid.uuid4())
        qty = parse_quantity(quantity, f"line_{n}", errors)
        if item is None or not item.is_active:
            errors[f"line_{n}"] = "Choose an item."
        elif f"line_{n}" not in errors and qty <= 0:
            errors[f"line_{n}"] = "Enter how many."
        else:
            parsed.append((item.id, qty))
    return parsed


def save(db: Session, actor: Actor, request_id: uuid.UUID | None, data: RequestInput) -> uuid.UUID:
    employee = _mine(db, actor)
    row = _row(db, request_id, lock=True) if request_id else None
    if row is not None and (row.employee_id != employee or row.status != "draft"):
        raise Conflict("Only your own draft can be changed.")
    errors: dict[str, str] = {}
    warehouse = db.get(Warehouse, uuid_or_none(data.warehouse_id) or uuid.uuid4())
    if warehouse is None or not warehouse.is_active:
        errors["warehouse_id"] = "Choose the store to take it from."
    if not data.purpose.strip():
        errors["purpose"] = "Say what it's for."
    if data.needed_by and data.needed_by < clock.today():
        errors["needed_by"] = "Choose today or later."
    parsed = _parse_lines(db, data.lines, errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one item."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert warehouse is not None  # noqa: S101
    if row is None:
        row = MaterialRequest(
            employee_id=employee,
            warehouse_id=warehouse.id,
            purpose=data.purpose.strip(),
            created_by=actor.user_id,
            cost_centre_id=org.cost_centre_of(db, employee),
        )
        db.add(row)
        db.flush()
    row.warehouse_id, row.needed_by, row.purpose = (
        warehouse.id,
        data.needed_by,
        data.purpose.strip(),
    )
    for old in _lines(db, row.id):
        db.delete(old)
    db.flush()
    for position, (item_id, qty) in enumerate(parsed, start=1):
        db.add(
            MaterialRequestLine(request_id=row.id, position=position, item_id=item_id, quantity=qty)
        )
    db.flush()
    record_by(
        db, actor, "inventory.request.save", entity_type="material_request", entity_id=str(row.id)
    )
    return row.id


def submit(db: Session, actor: Actor, request_id: uuid.UUID) -> str:
    employee = _mine(db, actor)
    row = _row(db, request_id, lock=True)
    if row.employee_id != employee or row.status != "draft":
        raise Conflict("Only your own draft can be submitted.")
    if not _lines(db, row.id):
        raise Conflict("Add at least one item first.")
    chain = org.approval_chain(db, employee, 1)
    row.approver_employee_id = chain[0] if chain else None
    row.number = numbering.next_number(db, "material_request")
    row.status, row.submitted_at = "submitted", clock.now()
    db.flush()
    record_by(
        db, actor, "inventory.request.submit", entity_type="material_request", entity_id=str(row.id)
    )
    return row.number


def cancel(db: Session, actor: Actor, request_id: uuid.UUID) -> None:
    employee = _mine(db, actor)
    row = _row(db, request_id, lock=True)
    if row.employee_id != employee or row.status not in ("draft", "submitted"):
        raise Conflict("Only your own request, before it's decided, can be cancelled.")
    if row.status == "draft":
        db.delete(row)
    else:
        row.status = "cancelled"
    db.flush()
    record_by(
        db,
        actor,
        "inventory.request.cancel",
        entity_type="material_request",
        entity_id=str(request_id),
    )


def _owner_user(db: Session, row: MaterialRequest) -> uuid.UUID | None:
    return people.accounts_of(db, {row.employee_id}).get(row.employee_id)


def _decide_permission(db: Session, actor: Actor, row: MaterialRequest) -> str | None:
    """The permission the actor would decide with, if any (never their own request)."""
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)

    def clear(permission: str) -> bool:
        return not sod.violations(
            catalogue.sod_rules(),
            permission,
            actor.user_id,
            {"owner_user_id": _owner_user(db, row)},
            held,
        )

    if row.approver_employee_id and _can(db, actor, APPROVE, row.employee_id):
        named = people.accounts_of(db, {row.approver_employee_id}).get(row.approver_employee_id)
        if (
            named is not None
            and (named == actor.user_id or named in authz.delegators(db, actor.user_id, APPROVE))
            and clear(APPROVE)
        ):
            return APPROVE
    if _can(db, actor, APPROVE_ANY) and clear(APPROVE_ANY):
        return APPROVE_ANY
    return None


def decide(db: Session, actor: Actor, request_id: uuid.UUID, *, approve: bool, note: str) -> None:
    row = _row(db, request_id, lock=True)
    if row.status != "submitted":
        raise Conflict("This request isn't waiting for a decision.")
    if _owner_user(db, row) == actor.user_id:
        raise PermissionDenied("You can't approve your own material request.")
    if _decide_permission(db, actor, row) is None:
        raise PermissionDenied("This request is for the requester's line manager to decide.")
    if not approve and not note.strip():
        raise ValidationFailed(field_errors={"note": "Say why it's rejected."})
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decided_at = actor.user_id, clock.now()
    row.decision_note = note.strip() or None
    db.flush()
    record_by(
        db,
        actor,
        f"inventory.request.{'approve' if approve else 'reject'}",
        entity_type="material_request",
        entity_id=str(row.id),
    )


def issue(
    db: Session,
    actor: Actor,
    request_id: uuid.UUID,
    *,
    quantities: dict[str, str],
    account_id: str,
    on: date | None,
) -> str:
    """Issue some or all of what is still to issue, as a posted issue document. Returns
    its number."""
    if not can(db, actor, documents.MOVE):
        raise PermissionDenied()
    row = _row(db, request_id, lock=True)
    if row.status != "approved":
        raise Conflict("Only an approved request can be issued.")
    errors: dict[str, str] = {}
    lines = {str(x.id): x for x in _lines(db, row.id)}
    taken: list[tuple[MaterialRequestLine, Decimal]] = []
    for key, text in quantities.items():
        line = lines.get(key)
        if line is None or not text.strip():
            continue
        qty = parse_quantity(text, f"q_{key}", errors)
        if f"q_{key}" in errors:
            continue
        left = line.quantity - line.issued
        if qty > left:
            errors[f"q_{key}"] = f"Only {stock.show(left)} is still to issue."
        elif qty > 0:
            taken.append((line, qty))
    if not taken and not errors:
        errors["form"] = "Enter what is being issued on at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    doc_id = documents.save(
        db,
        actor,
        None,
        documents.DocumentInput(
            kind="issue",
            doc_date=on or clock.today(),
            warehouse_id=str(row.warehouse_id),
            cost_centre_id=str(row.cost_centre_id or ""),
            account_id=account_id,
            reason=f"Material request {row.number}",
            lines=tuple(documents.LineInput(str(line.item_id), str(qty)) for line, qty in taken),
        ),
    )
    doc = db.get(StockDocument, doc_id)
    assert doc is not None  # noqa: S101
    doc.request_id = row.id
    for doc_line, (line, qty) in zip(documents.lines_of(db, doc_id), taken, strict=True):
        doc_line.request_line_id = line.id
        line.issued += qty
    number = documents.post(db, actor, doc_id)
    if all(x.issued >= x.quantity for x in lines.values()):
        row.status = "issued"
    db.flush()
    record_by(
        db,
        actor,
        "inventory.request.issue",
        entity_type="material_request",
        entity_id=str(row.id),
        changes={"issue": number},
    )
    return number


# ------------------------------------------------------------------ views


def shown_status(row: MaterialRequest, lines: list[MaterialRequestLine]) -> str:
    if row.status == "approved" and any(x.issued > 0 for x in lines):
        return "partly"
    return row.status


@dataclass(frozen=True, slots=True)
class RequestRow:
    id: uuid.UUID
    number: str | None
    requester: str
    needed_by: date | None
    lines: int
    status: str
    created_at: datetime


def list_requests(db: Session, actor: Actor, tab: str) -> list[RequestRow]:
    query = select(MaterialRequest).order_by(MaterialRequest.created_at.desc())
    me = people.employee_of_user(db, actor.user_id)
    if tab == "mine":
        if me is None:
            return []
        query = query.where(MaterialRequest.employee_id == me)
    elif tab == "approve":
        query = query.where(MaterialRequest.status == "submitted")
    elif tab == "issue":
        if not can(db, actor, documents.MOVE):
            return []
        query = query.where(MaterialRequest.status == "approved")
    else:
        query = query.where(engine.scope_filter(db, actor.user_id, READ, MaterialRequest))
    rows = list(db.scalars(query.limit(300)))
    if tab == "approve":
        rows = [r for r in rows if _decide_permission(db, actor, r) is not None]
    names = people.names(db, {r.employee_id for r in rows})
    counts = {}
    for r in rows:
        lines = _lines(db, r.id)
        counts[r.id] = (len(lines), shown_status(r, lines))
    return [
        RequestRow(
            r.id,
            r.number,
            names.get(r.employee_id, ""),
            r.needed_by,
            counts[r.id][0],
            counts[r.id][1],
            r.created_at,
        )
        for r in rows
    ]


def counts(db: Session, actor: Actor) -> dict[str, int]:
    return {
        "approve": len(list_requests(db, actor, "approve")),
        "issue": len(list_requests(db, actor, "issue")),
    }


@dataclass(frozen=True, slots=True)
class LineView:
    id: uuid.UUID
    item_id: uuid.UUID
    code: str
    name: str
    unit: str
    quantity: Decimal
    issued: Decimal
    on_hand: Decimal

    @property
    def left(self) -> Decimal:
        return max(self.quantity - self.issued, ZERO)


@dataclass(frozen=True, slots=True)
class RequestView:
    request: MaterialRequest
    requester: str
    approver: str | None
    decided_by: str | None
    warehouse: str
    lines: list[LineView]
    status: str
    issues: list[tuple[uuid.UUID, str]]
    is_mine: bool
    can_edit: bool
    can_decide: bool
    can_issue: bool


def get(db: Session, actor: Actor, request_id: uuid.UUID) -> RequestView:
    row = _row(db, request_id)
    mine = people.employee_of_user(db, actor.user_id) == row.employee_id
    can_decide = (
        row.status == "submitted" and not mine and _decide_permission(db, actor, row) is not None
    )
    can_issue = row.status == "approved" and can(db, actor, documents.MOVE)
    if not (mine or can_decide or can_issue or _can(db, actor, READ, row.employee_id)):
        raise NotFound()
    lines = _lines(db, row.id)
    items = {
        i.id: i for i in db.scalars(select(Item).where(Item.id.in_([x.item_id for x in lines])))
    }
    unit_codes = dict(db.execute(select(Unit.id, Unit.code)).all())
    names = people.names(
        db, {row.employee_id} | ({row.approver_employee_id} if row.approver_employee_id else set())
    )
    deciders = documents.finance_user_names(db, {row.decided_by})
    warehouse = db.get(Warehouse, row.warehouse_id)
    issues = [
        (d.id, d.number or "")
        for d in db.scalars(
            select(StockDocument)
            .where(StockDocument.request_id == row.id)
            .order_by(StockDocument.created_at)
        )
    ]
    return RequestView(
        row,
        names.get(row.employee_id, ""),
        names.get(row.approver_employee_id) if row.approver_employee_id else None,
        deciders.get(row.decided_by) if row.decided_by else None,
        warehouse.name if warehouse else "",
        [
            LineView(
                x.id,
                x.item_id,
                items[x.item_id].code,
                items[x.item_id].name,
                unit_codes.get(items[x.item_id].unit_id, ""),
                x.quantity,
                x.issued,
                stock.on_hand(db, x.item_id, row.warehouse_id),
            )
            for x in lines
        ],
        shown_status(row, lines),
        issues,
        mine,
        mine and row.status == "draft",
        can_decide,
        can_issue,
    )


def issued_lines(db: Session, document_id: uuid.UUID) -> list[StockDocumentLine]:
    return documents.lines_of(db, document_id)
