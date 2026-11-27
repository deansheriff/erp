"""Purchase requisitions (ADR 0030). Any employee asks for goods or services for their
department. Submitting numbers it and sets the approval steps its estimated total needs:
the line manager always, then Finance and the Director over their tiers' amounts. Steps
are decided in order, each by a different person, never the requester; a rejection at any
step ends it. An award over the approved tier adds the missing steps (`require_for`)."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.authz import service as authz
from app.authz.engine import Resource
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.inventory import service as inventory
from app.modules.procurement import settings as setup
from app.modules.procurement.models import Requisition, RequisitionLine, RequisitionStep, Rfq
from app.org import service as org

CREATE = "procurement.requisition.create"
READ = "procurement.requisition.read"
APPROVE = "procurement.requisition.approve"
APPROVE_ANY = "procurement.requisition.approve_any"
LEVEL_PERMISSIONS = {
    "finance": "procurement.requisition.approve_finance",
    "director": "procurement.requisition.approve_director",
}
RFQ_READ = "procurement.rfq.read"
ZERO = Decimal(0)
CENT = Decimal("0.01")
STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Waiting for approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "ordered": "Ordered",
}
STATUS_TONES = {
    "draft": "neutral",
    "submitted": "warning",
    "approved": "info",
    "rejected": "danger",
    "cancelled": "neutral",
    "ordered": "success",
}


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def row_of(db: Session, requisition_id: uuid.UUID, *, lock: bool = False) -> Requisition:
    query = select(Requisition).where(Requisition.id == requisition_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def lines_of(db: Session, requisition_id: uuid.UUID) -> list[RequisitionLine]:
    return list(
        db.scalars(
            select(RequisitionLine)
            .where(RequisitionLine.requisition_id == requisition_id)
            .order_by(RequisitionLine.position)
        )
    )


def steps_of(db: Session, requisition_id: uuid.UUID) -> list[RequisitionStep]:
    return list(
        db.scalars(
            select(RequisitionStep)
            .where(RequisitionStep.requisition_id == requisition_id)
            .order_by(RequisitionStep.position)
        )
    )


def current_step(db: Session, requisition_id: uuid.UUID) -> RequisitionStep | None:
    return next((s for s in steps_of(db, requisition_id) if s.status == "pending"), None)


def live_rfq(db: Session, requisition_id: uuid.UUID) -> Rfq | None:
    return db.scalars(
        select(Rfq).where(Rfq.requisition_id == requisition_id, Rfq.status != "cancelled")
    ).first()


def _mine(db: Session, actor: Actor) -> uuid.UUID:
    employee = people.employee_of_user(db, actor.user_id)
    if employee is None or not _can(db, actor, CREATE, employee):
        raise PermissionDenied("Purchase requisitions are for people on the staff list.")
    return employee


# ------------------------------------------------------------------ drafting


@dataclass(frozen=True, slots=True)
class LineInput:
    item_id: str
    warehouse_id: str
    description: str
    account_id: str
    quantity: str
    estimated_price: str


@dataclass(frozen=True, slots=True)
class RequisitionInput:
    needed_by: date | None
    purpose: str
    lines: tuple[LineInput, ...]


@dataclass(frozen=True, slots=True)
class _Parsed:
    item_id: uuid.UUID | None
    warehouse_id: uuid.UUID | None
    description: str
    account_id: uuid.UUID | None
    quantity: Decimal
    price: Decimal


def _quantity(text: str, key: str, errors: dict[str, str]) -> Decimal:
    try:
        value = Decimal((text or "1").strip().replace(",", ""))
    except InvalidOperation:
        value = ZERO
    if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.001")):
        errors[key] = "Enter how many, like 12 or 2.5."
        return ZERO
    return value


def _blank(line: LineInput) -> bool:
    return not (
        line.item_id or line.description.strip() or line.account_id or line.estimated_price.strip()
    )


def _parse_line(
    db: Session, line: LineInput, key: str, errors: dict[str, str], accounts: set[uuid.UUID]
) -> _Parsed | None:
    quantity = _quantity(line.quantity, key, errors)
    price = setup.parse_money(line.estimated_price, key, errors, label="an estimated unit price")
    item_id = setup.uuid_or_none(line.item_id)
    item = inventory.LINK.item(db, item_id) if item_id else None
    warehouse = setup.uuid_or_none(line.warehouse_id)
    account = setup.uuid_or_none(line.account_id)
    description = line.description.strip() or (item.name if item else "")
    if key in errors:
        return None
    if item_id and (item is None or not item.is_active):
        errors[key] = "Choose an active stock item."
    elif item and warehouse not in {w for w, _ in inventory.LINK.warehouses(db)}:
        errors[key] = "Choose the warehouse it goes into."
    elif not description:
        errors[key] = "Say what's needed."
    elif not line.estimated_price.strip():
        errors[key] = "Enter an estimated unit price."
    elif account and account not in accounts:
        errors[key] = "Choose an expense account, or leave it blank."
    else:
        return _Parsed(
            item.id if item else None,
            warehouse if item else None,
            description[:300],
            None if item else account,
            quantity,
            price,
        )
    return None


def _parse_lines(
    db: Session, lines: tuple[LineInput, ...], errors: dict[str, str]
) -> list[_Parsed]:
    accounts = {a.id for a in finance.purchase_accounts(db)}
    parsed = []
    for n, line in enumerate(lines):
        if _blank(line):
            continue
        found = _parse_line(db, line, f"line_{n}", errors, accounts)
        if found:
            parsed.append(found)
    return parsed


def _total(lines: list[_Parsed] | list[RequisitionLine]) -> Decimal:
    total = ZERO
    for line in lines:
        price = line.price if isinstance(line, _Parsed) else line.estimated_price
        total += (line.quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)
    return total


def save(
    db: Session, actor: Actor, requisition_id: uuid.UUID | None, data: RequisitionInput
) -> uuid.UUID:
    employee = _mine(db, actor)
    row = row_of(db, requisition_id, lock=True) if requisition_id else None
    if row is not None and (row.employee_id != employee or row.status != "draft"):
        raise Conflict("Only your own draft can be changed.")
    errors: dict[str, str] = {}
    if not data.purpose.strip():
        errors["purpose"] = "Say what it's for."
    if data.needed_by and data.needed_by < clock.today():
        errors["needed_by"] = "Choose today or later."
    parsed = _parse_lines(db, data.lines, errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if row is None:
        row = Requisition(
            employee_id=employee,
            purpose=data.purpose.strip(),
            created_by=actor.user_id,
            cost_centre_id=org.cost_centre_of(db, employee),
        )
        db.add(row)
        db.flush()
    row.needed_by, row.purpose = data.needed_by, data.purpose.strip()
    row.estimated_total = _total(parsed)
    for old in lines_of(db, row.id):
        db.delete(old)
    db.flush()
    for position, p in enumerate(parsed, start=1):
        db.add(
            RequisitionLine(
                requisition_id=row.id,
                position=position,
                item_id=p.item_id,
                warehouse_id=p.warehouse_id,
                description=p.description,
                account_id=p.account_id,
                quantity=p.quantity,
                estimated_price=p.price,
            )
        )
    db.flush()
    record_by(
        db,
        actor,
        "procurement.requisition.save",
        entity_type="purchase_requisition",
        entity_id=str(row.id),
        changes={"total": str(row.estimated_total), "lines": len(parsed)},
    )
    return row.id


def _add_steps(db: Session, row: Requisition, levels: list[str], start: int) -> None:
    for position, level in enumerate(levels, start=start):
        manager = None
        if level == "line_manager":
            chain = org.approval_chain(db, row.employee_id, 1)
            manager = chain[0] if chain else None
        db.add(
            RequisitionStep(
                requisition_id=row.id,
                position=position,
                level=level,
                approver_employee_id=manager,
            )
        )
    db.flush()


def submit(db: Session, actor: Actor, requisition_id: uuid.UUID) -> str:
    employee = _mine(db, actor)
    row = row_of(db, requisition_id, lock=True)
    if row.employee_id != employee or row.status != "draft":
        raise Conflict("Only your own draft can be submitted.")
    lines = lines_of(db, row.id)
    if not lines:
        raise Conflict("Add at least one line first.")
    row.estimated_total = _total(lines)
    _add_steps(db, row, setup.levels_for(db, row.estimated_total), 1)
    row.number = numbering.next_number(db, "purchase_requisition")
    row.status, row.submitted_at = "submitted", clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "procurement.requisition.submit",
        entity_type="purchase_requisition",
        entity_id=str(row.id),
        changes={"number": row.number, "total": str(row.estimated_total)},
    )
    return row.number


def cancel(db: Session, actor: Actor, requisition_id: uuid.UUID) -> None:
    employee = _mine(db, actor)
    row = row_of(db, requisition_id, lock=True)
    if row.employee_id != employee or row.status not in ("draft", "submitted", "approved"):
        raise Conflict("Only your own requisition, before it's ordered, can be cancelled.")
    if live_rfq(db, row.id) is not None:
        raise Conflict("Procurement is already asking for quotes. Ask them to cancel the RFQ.")
    if row.status == "draft":
        db.delete(row)
    else:
        row.status = "cancelled"
    db.flush()
    record_by(
        db,
        actor,
        "procurement.requisition.cancel",
        entity_type="purchase_requisition",
        entity_id=str(requisition_id),
    )


# ------------------------------------------------------------------ approving


def _owner_user(db: Session, row: Requisition) -> uuid.UUID | None:
    return people.accounts_of(db, {row.employee_id}).get(row.employee_id)


def _clear(db: Session, actor: Actor, row: Requisition, permission: str) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        permission,
        actor.user_id,
        {"owner_user_id": _owner_user(db, row)},
        held,
    )


def _line_manager_may(db: Session, actor: Actor, row: Requisition, step: RequisitionStep) -> bool:
    if step.approver_employee_id is None:  # no line manager: the Director steps in
        return _can(db, actor, APPROVE_ANY) and _clear(db, actor, row, APPROVE_ANY)
    if not _can(db, actor, APPROVE, row.employee_id):
        return False
    named = people.accounts_of(db, {step.approver_employee_id}).get(step.approver_employee_id)
    return (
        named is not None
        and (named == actor.user_id or named in authz.delegators(db, actor.user_id, APPROVE))
        and _clear(db, actor, row, APPROVE)
    )


def may_decide(db: Session, actor: Actor, row: Requisition, step: RequisitionStep) -> bool:
    """Whether the actor may decide this step: never their own requisition, and never a
    second step of one they've already approved."""
    if _owner_user(db, row) == actor.user_id:
        return False
    if any(s.decided_by == actor.user_id for s in steps_of(db, row.id) if s.status == "approved"):
        return False
    if step.level == "line_manager":
        return _line_manager_may(db, actor, row, step)
    permission = LEVEL_PERMISSIONS[step.level]
    return _can(db, actor, permission) and _clear(db, actor, row, permission)


def decide(
    db: Session, actor: Actor, requisition_id: uuid.UUID, *, approve: bool, note: str
) -> str:
    """Decide the current step. Returns the requisition's status afterwards."""
    row = row_of(db, requisition_id, lock=True)
    step = current_step(db, row.id) if row.status == "submitted" else None
    if step is None:
        raise Conflict("This requisition isn't waiting for a decision.")
    if _owner_user(db, row) == actor.user_id:
        raise PermissionDenied("You can't approve your own requisition.")
    if not may_decide(db, actor, row, step):
        raise PermissionDenied(
            f"This step is for {_waiting_for(db, step)}, and each step needs a different person."
        )
    if not approve and not note.strip():
        raise ValidationFailed(field_errors={"note": "Say why it's rejected."})
    step.status = "approved" if approve else "rejected"
    step.decided_by, step.decided_at = actor.user_id, clock.now()
    step.note = note.strip() or None
    db.flush()
    if not approve:
        row.status = "rejected"
    elif current_step(db, row.id) is None:
        row.status = "approved"
    db.flush()
    record_by(
        db,
        actor,
        f"procurement.requisition.{'approve' if approve else 'reject'}",
        entity_type="purchase_requisition",
        entity_id=str(row.id),
        changes={"step": step.level, "status": row.status},
    )
    return row.status


def require_for(db: Session, row: Requisition, amount: Decimal) -> list[str]:
    """Add the approval steps an amount needs that the requisition hasn't had. Returns the
    levels added; with any, the requisition waits for them again (ADR 0030)."""
    steps = steps_of(db, row.id)
    have = {s.level for s in steps if s.status in ("approved", "pending")}
    missing = [level for level in setup.levels_for(db, amount) if level not in have]
    if missing:
        _add_steps(db, row, missing, (steps[-1].position if steps else 0) + 1)
        row.status = "submitted"
        db.flush()
    return missing


def _waiting_for(db: Session, step: RequisitionStep) -> str:
    if step.level == "line_manager":
        if step.approver_employee_id is None:
            return "a Director (there's no line manager)"
        name = people.names(db, {step.approver_employee_id}).get(step.approver_employee_id)
        return f"{name}, the line manager" if name else "the line manager"
    return "a Finance Manager" if step.level == "finance" else "a Director"


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class RequisitionRow:
    id: uuid.UUID
    number: str | None
    requester: str
    purpose: str
    needed_by: date | None
    total: Decimal
    step: str | None  # "Finance Manager · 2 of 3" while waiting
    status: str
    created_at: datetime


def _step_label(steps: list[RequisitionStep]) -> str | None:
    pending = next((s for s in steps if s.status == "pending"), None)
    if pending is None:
        return None
    return f"{setup.LEVEL_LABELS[pending.level]} · {pending.position} of {len(steps)}"


def list_requisitions(db: Session, actor: Actor, tab: str) -> list[RequisitionRow]:
    query = select(Requisition).order_by(Requisition.created_at.desc())
    me = people.employee_of_user(db, actor.user_id)
    if tab == "mine":
        if me is None:
            return []
        query = query.where(Requisition.employee_id == me)
    elif tab == "approve":
        query = query.where(Requisition.status == "submitted")
    elif tab == "sourcing":
        if not _can(db, actor, RFQ_READ):
            return []
        query = query.where(Requisition.status.in_(("approved", "ordered")))
    else:
        query = query.where(
            Requisition.status != "draft",
            engine.scope_filter(db, actor.user_id, READ, Requisition),
        )
    rows = list(db.scalars(query.limit(300)))
    steps = {r.id: steps_of(db, r.id) for r in rows}
    if tab == "approve":
        rows = [
            r
            for r in rows
            if (step := next((s for s in steps[r.id] if s.status == "pending"), None))
            and may_decide(db, actor, r, step)
        ]
    names = people.names(db, {r.employee_id for r in rows})
    return [
        RequisitionRow(
            r.id,
            r.number,
            names.get(r.employee_id, ""),
            r.purpose,
            r.needed_by,
            r.estimated_total,
            _step_label(steps[r.id]) if r.status == "submitted" else None,
            r.status,
            r.created_at,
        )
        for r in rows
    ]


def to_approve(db: Session, actor: Actor) -> int:
    return len(list_requisitions(db, actor, "approve"))


@dataclass(frozen=True, slots=True)
class LineView:
    id: uuid.UUID
    item_id: uuid.UUID | None
    item: str | None
    warehouse_id: uuid.UUID | None
    warehouse: str | None
    description: str
    account_id: uuid.UUID | None
    account: str | None
    quantity: Decimal
    estimated_price: Decimal

    @property
    def estimated_total(self) -> Decimal:
        return (self.quantity * self.estimated_price).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class StepView:
    position: int
    level: str
    label: str
    status: str
    waiting_for: str
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    above: Decimal | None  # the tier's amount, for Finance and Director


@dataclass(frozen=True, slots=True)
class RequisitionView:
    row: Requisition
    requester: str
    cost_centre: str | None
    lines: list[LineView]
    steps: list[StepView]
    rfq: tuple[uuid.UUID, str, str] | None  # id, number, status
    is_mine: bool
    can_edit: bool
    can_cancel: bool
    can_decide: bool
    can_start_rfq: bool


def line_views(db: Session, lines: list[RequisitionLine]) -> list[LineView]:
    items = {}
    for line in lines:
        if line.item_id and line.item_id not in items:
            found = inventory.LINK.item(db, line.item_id)
            items[line.item_id] = found.label if found else ""
    stores = dict(inventory.LINK.warehouses(db))
    accounts = {a.id: f"{a.code} {a.name}" for a in finance.all_accounts(db)}
    return [
        LineView(
            x.id,
            x.item_id,
            items.get(x.item_id) if x.item_id else None,
            x.warehouse_id,
            stores.get(x.warehouse_id, "") if x.warehouse_id else None,
            x.description,
            x.account_id,
            accounts.get(x.account_id) if x.account_id else None,
            x.quantity,
            x.estimated_price,
        )
        for x in lines
    ]


def _step_views(db: Session, steps: list[RequisitionStep]) -> list[StepView]:
    deciders = setup.user_names(db, {s.decided_by for s in steps})
    above = {t.level: t.above for t in setup.tiers(db)}
    return [
        StepView(
            s.position,
            s.level,
            setup.LEVEL_LABELS[s.level],
            s.status,
            _waiting_for(db, s),
            deciders.get(s.decided_by) if s.decided_by else None,
            s.decided_at,
            s.note,
            above.get(s.level) if s.level != "line_manager" else None,
        )
        for s in steps
    ]


def get(db: Session, actor: Actor, requisition_id: uuid.UUID) -> RequisitionView:
    row = row_of(db, requisition_id)
    mine = people.employee_of_user(db, actor.user_id) == row.employee_id
    steps = steps_of(db, row.id)
    pending = next((s for s in steps if s.status == "pending"), None)
    can_decide = (
        row.status == "submitted"
        and not mine
        and pending is not None
        and may_decide(db, actor, row, pending)
    )
    sourcing = _can(db, actor, RFQ_READ) and row.status != "draft"
    if not (mine or can_decide or sourcing or _can(db, actor, READ, row.employee_id)):
        raise NotFound()
    if row.status == "draft" and not mine:
        raise NotFound()
    rfq = live_rfq(db, row.id)
    centres = dict(org.cost_centre_choices(db))
    return RequisitionView(
        row,
        people.names(db, {row.employee_id}).get(row.employee_id, ""),
        centres.get(row.cost_centre_id) if row.cost_centre_id else None,
        line_views(db, lines_of(db, row.id)),
        _step_views(db, steps),
        (rfq.id, rfq.number, rfq.status) if rfq else None,
        mine,
        mine and row.status == "draft",
        mine and row.status in ("draft", "submitted", "approved") and rfq is None,
        can_decide,
        row.status == "approved" and rfq is None and _can(db, actor, "procurement.rfq.manage"),
    )
