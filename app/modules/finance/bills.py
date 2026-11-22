"""Supplier bills and debit notes (ADR 0025). Every bill is drafted by one person and
approved by another. A bill from a purchase order is matched three ways on approval:
quantities billed against quantities received, prices against the PO within the
tolerance. One that fails is held until a Finance Manager posts it with a reason. A debit
note reduces the bill it is raised against and is issued by someone other than its
drafter. Posting creates the journal in the same transaction."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, journals, payables, periods, purchasing, sales, tax
from app.modules.finance.models import (
    Bill,
    BillLine,
    Journal,
    PurchaseLine,
    PurchaseOrder,
    Supplier,
    SupplierPayment,
    SupplierPaymentLine,
    WhtType,
)
from app.modules.finance.purchasing import APPROVE, CREATE, READ, cost_accounts, price_line
from app.org import service as org

OVERRIDE = "finance.bill.override"
ZERO = Decimal(0)
HUNDRED = Decimal(100)
MIN_REASON = 5
KIND_LABELS = {"bill": "Bill", "debit_note": "Debit note"}
SEQUENCES = {"bill": "bill", "debit_note": "debit_note"}
STATUS_LABELS = {
    "draft": "Draft",
    "held": "Held: match failed",
    "posted": "Posted",
    "part_paid": "Part paid",
    "paid": "Paid",
    "overdue": "Overdue",
}
STATUS_TONES = {
    "draft": "neutral",
    "held": "danger",
    "posted": "info",
    "part_paid": "warning",
    "paid": "success",
    "overdue": "danger",
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


# ------------------------------------------------------------------ input


@dataclass(frozen=True, slots=True)
class BillLineInput:
    order_line_id: str
    description: str
    account_id: str
    cost_centre_id: str
    quantity: str
    unit_price: str
    vat_code: str


@dataclass(frozen=True, slots=True)
class BillInput:
    kind: str
    supplier_id: str
    supplier_reference: str
    bill_date: date | None
    due_date: date | None
    wht_type_id: str
    memo: str
    lines: list[BillLineInput]


def _locked(db: Session, bill_id: uuid.UUID) -> Bill:
    row = db.scalars(select(Bill).where(Bill.id == bill_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def bill_lines(db: Session, bill_id: uuid.UUID) -> list[BillLine]:
    return list(
        db.scalars(select(BillLine).where(BillLine.bill_id == bill_id).order_by(BillLine.position))
    )


def _parse(
    db: Session, row: Bill | None, lines: list[BillLineInput]
) -> tuple[list[tuple[sales.ParsedLine, uuid.UUID | None]], dict[str, str]]:
    """Lines as (parsed, PO line). On a bill from a PO, each line keeps its PO line's
    account; the PO line must belong to that order."""
    allowed = cost_accounts(db)
    order_lines = (
        {line.id: line for line in purchasing.order_lines(db, row.order_id)}
        if row is not None and row.order_id
        else {}
    )
    parsed_input = []
    for line in lines:
        po_line = order_lines.get(_uuid(line.order_line_id)) if line.order_line_id else None  # type: ignore[arg-type]
        account = str(po_line.account_id) if po_line else line.account_id
        parsed_input.append(
            sales.LineInput(
                line.description,
                account,
                line.cost_centre_id,
                line.quantity,
                line.unit_price,
                line.vat_code,
            )
        )
    parsed, errors = sales.parse_lines(db, parsed_input, allowed=allowed)
    if errors:
        return [], errors
    # With no errors, the parsed lines are the non-blank rows, in order.
    kept = [
        line
        for line in lines
        if line.description.strip() or line.account_id or line.unit_price.strip()
    ]
    out = []
    for p, line in zip(parsed, kept, strict=True):
        po_id = _uuid(line.order_line_id)
        out.append((p, po_id if po_id in order_lines else None))
    return out, errors


def save_bill(db: Session, actor: Actor, bill_id: uuid.UUID | None, data: BillInput) -> uuid.UUID:
    """Create or change a draft or held bill or debit note (a held one goes back to draft)."""
    _require(db, actor, CREATE)
    row = _locked(db, bill_id) if bill_id else None
    if row is not None and row.status == "posted":
        raise Conflict("A posted bill can't be changed. Raise a debit note instead.")
    kind = row.kind if row is not None else data.kind
    if kind not in KIND_LABELS:
        raise ValidationFailed(field_errors={"form": "Choose a bill or a debit note."})
    errors: dict[str, str] = {}
    supplier = db.get(Supplier, _uuid(data.supplier_id)) if _uuid(data.supplier_id) else None
    if row is not None and (row.order_id or row.bill_id):
        supplier = db.get(Supplier, row.supplier_id)  # tied to its order or bill
    if supplier is None or not supplier.is_active:
        errors["supplier_id"] = "Choose an active supplier."
    if data.bill_date is None:
        errors["bill_date"] = "Enter the date on the supplier's invoice."
    if data.due_date and data.bill_date and data.due_date < data.bill_date:
        errors["due_date"] = "The due date can't be before the bill date."
    reference = data.supplier_reference.strip()[:80]
    if kind == "bill" and not reference:
        errors["supplier_reference"] = "Enter the supplier's invoice number."
    elif kind == "bill" and supplier and _reference_taken(db, supplier.id, reference, bill_id):
        errors["supplier_reference"] = "This supplier invoice number is already recorded."
    wht = _uuid(data.wht_type_id)
    if data.wht_type_id and (wht is None or db.get(WhtType, wht) is None):
        errors["wht_type_id"] = "Choose a withholding tax type."
    parsed, line_errors = _parse(db, row, data.lines)
    errors.update(line_errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert supplier is not None and data.bill_date is not None  # noqa: S101  (checked above)
    if row is None:
        row = Bill(kind=kind, status="draft", supplier_id=supplier.id, bill_date=data.bill_date)
        db.add(row)
    row.supplier_id, row.bill_date = supplier.id, data.bill_date
    row.supplier_reference = reference or None
    row.due_date = (
        (data.due_date or data.bill_date + timedelta(days=supplier.payment_terms_days))
        if kind == "bill"
        else None
    )
    row.wht_type_id = wht if kind == "bill" else None
    row.memo = data.memo.strip()[:200] or None
    row.status, row.hold_reason = "draft", None
    row.created_by = actor.user_id  # whoever last drafted it can't approve it
    db.flush()
    _replace_lines(db, row, parsed)
    record_by(
        db,
        actor,
        f"finance.{kind}.draft",
        entity_type="bill",
        entity_id=str(row.id),
        changes={"lines": {"from": None, "to": len(parsed)}},
    )
    return row.id


def _reference_taken(
    db: Session, supplier_id: uuid.UUID, reference: str, bill_id: uuid.UUID | None
) -> bool:
    query = select(Bill.id).where(
        Bill.kind == "bill",
        Bill.supplier_id == supplier_id,
        func.lower(Bill.supplier_reference) == reference.lower(),
    )
    if bill_id:
        query = query.where(Bill.id != bill_id)
    return db.scalar(query) is not None


def _replace_lines(
    db: Session, row: Bill, parsed: list[tuple[sales.ParsedLine, uuid.UUID | None]]
) -> None:
    for old in bill_lines(db, row.id):
        db.delete(old)
    db.flush()
    for position, (line, po_line) in enumerate(parsed, start=1):
        db.add(
            BillLine(
                bill_id=row.id,
                position=position,
                description=line.description,
                account_id=line.account.id,
                cost_centre_id=line.cost_centre_id,
                order_line_id=po_line,
                quantity=line.quantity,
                unit_price=line.unit_price,
                net=line.net,
                vat_code=line.vat_code,
                vat_rate=ZERO,
                vat=ZERO,
            )
        )
    db.flush()
    try:
        _price(db, row)
    except Conflict:  # no VAT rate yet: approving will say so
        db.flush()


def _price(db: Session, row: Bill) -> None:
    rate = tax.vat_rate_on(db, row.bill_date)
    net = vat = ZERO
    for line in bill_lines(db, row.id):
        price_line(line, rate)
        net, vat = net + line.net, vat + line.vat
    row.net, row.vat, row.total = net, vat, net + vat
    db.flush()


def delete_bill(db: Session, actor: Actor, bill_id: uuid.UUID) -> str:
    _require(db, actor, CREATE)
    row = _locked(db, bill_id)
    if row.status == "posted":
        raise Conflict("A posted bill can't be deleted.")
    kind = row.kind
    record_by(db, actor, f"finance.{kind}.delete", entity_type="bill", entity_id=str(row.id))
    db.delete(row)
    db.flush()
    return kind


def bill_from_order(db: Session, actor: Actor, order_id: uuid.UUID, *, today: date) -> uuid.UUID:
    """A draft bill for what has been received on the order but not yet billed."""
    _require(db, actor, CREATE)
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise NotFound()
    if order.status != "approved":
        raise Conflict("Only an approved purchase order can be billed.")
    done = purchasing.progress(db, order.id)
    lines = [line for line in purchasing.order_lines(db, order.id) if done[line.id].to_bill > 0]
    if not lines:
        raise Conflict("Nothing received on this order is waiting to be billed.")
    supplier = db.get(Supplier, order.supplier_id)
    assert supplier is not None  # noqa: S101  (foreign key)
    row = Bill(
        kind="bill",
        status="draft",
        supplier_id=order.supplier_id,
        order_id=order.id,
        bill_date=today,
        due_date=today + timedelta(days=supplier.payment_terms_days),
        wht_type_id=supplier.wht_type_id,
        memo=f"Against {order.number}",
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for position, line in enumerate(lines, start=1):
        db.add(
            BillLine(
                bill_id=row.id,
                position=position,
                description=line.description,
                account_id=line.account_id,
                cost_centre_id=line.cost_centre_id,
                order_line_id=line.id,
                quantity=done[line.id].to_bill,
                unit_price=line.unit_price,
                net=ZERO,
                vat_code=line.vat_code,
                vat_rate=ZERO,
                vat=ZERO,
            )
        )
    db.flush()
    try:
        _price(db, row)
    except Conflict:
        db.flush()
    return row.id


def debit_note_for(db: Session, actor: Actor, bill_id: uuid.UUID, *, today: date) -> uuid.UUID:
    """A draft debit note against a posted bill, with its lines to trim."""
    _require(db, actor, CREATE)
    bill = db.get(Bill, bill_id)
    if bill is None or bill.kind != "bill":
        raise NotFound()
    if bill.status != "posted":
        raise Conflict("A debit note can only be raised against a posted bill.")
    row = Bill(
        kind="debit_note",
        status="draft",
        supplier_id=bill.supplier_id,
        bill_id=bill.id,
        order_id=bill.order_id,
        bill_date=today,
        memo=f"Against {bill.number}",
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for line in bill_lines(db, bill.id):
        db.add(
            BillLine(
                bill_id=row.id,
                position=line.position,
                description=line.description,
                account_id=line.account_id,
                cost_centre_id=line.cost_centre_id,
                order_line_id=line.order_line_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                net=line.net,
                vat_code=line.vat_code,
                vat_rate=ZERO,
                vat=ZERO,
            )
        )
    db.flush()
    try:
        _price(db, row)
    except Conflict:
        db.flush()
    return row.id


# ------------------------------------------------------------------ matching


@dataclass(frozen=True, slots=True)
class MatchResult:
    ok: bool
    message: str
    received: Decimal | None = None
    po_price: Decimal | None = None


def match(db: Session, row: Bill) -> dict[uuid.UUID, MatchResult]:
    """Three-way match of a bill's PO lines, by bill line. Direct lines aren't matched."""
    lines = bill_lines(db, row.id)
    po_ids = [line.order_line_id for line in lines if line.order_line_id]
    if row.kind != "bill" or not po_ids:
        return {}
    po_lines = {
        line.id: line
        for line in db.scalars(select(PurchaseLine).where(PurchaseLine.id.in_(po_ids)))
    }
    received = purchasing.received_quantities(db, po_ids)
    billed = purchasing.billed_quantities(db, po_ids, exclude_bill=row.id)
    tolerance = periods.settings(db).price_tolerance
    this_bill: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        if line.order_line_id:
            this_bill[line.order_line_id] = this_bill.get(line.order_line_id, ZERO) + line.quantity
    out = {}
    for line in lines:
        if not line.order_line_id or line.order_line_id not in po_lines:
            continue
        po = po_lines[line.order_line_id]
        got = received.get(po.id, ZERO)
        total_billed = billed.get(po.id, ZERO) + this_bill[po.id]
        problems = []
        if total_billed > got:
            problems.append(f"Billed {total_billed.normalize():f}, received {got.normalize():f}")
        if po.unit_price:
            diff = (line.unit_price - po.unit_price) / po.unit_price * HUNDRED
            if abs(diff) > tolerance:
                direction = "above" if diff > 0 else "below"
                problems.append(
                    f"Price {abs(diff).quantize(Decimal('0.1'))}% {direction} the PO, "
                    f"over the {tolerance.normalize():f}% tolerance"
                )
        elif line.unit_price:
            problems.append("The PO line is free of charge")
        out[line.id] = MatchResult(
            not problems, "; ".join(problems) or "Matched", got, po.unit_price
        )
    return out


# ------------------------------------------------------------------ approving


def _journal_lines(db: Session, row: Bill) -> list[journals.SystemLine]:
    """Bill: debit costs and VAT input, credit payables. A debit note is the reverse."""
    settings = periods.settings(db)
    codes = {a.id: a.code for a in accounts.all_accounts(db)}
    note = row.kind == "debit_note"
    sides = [
        (codes[line.account_id], line.cost_centre_id, line.net) for line in bill_lines(db, row.id)
    ]
    sides.append((settings.vat_input_code, None, row.vat))
    out: list[journals.SystemLine] = [
        (settings.payables_code, None, row.total, ZERO)
        if note
        else (settings.payables_code, None, ZERO, row.total)
    ]
    for code, centre, amount in sides:
        if amount:
            out.append((code, centre, ZERO, amount) if note else (code, centre, amount, ZERO))
    return out


def _check_debit_note(db: Session, row: Bill) -> None:
    bill = db.get(Bill, row.bill_id) if row.bill_id else None
    if bill is None or bill.status != "posted":
        raise Conflict("A debit note must be against a posted bill.")
    left = bill.total - payables.paid_amounts(db, [bill.id]).get(bill.id, ZERO)
    if row.total > left:
        raise Conflict(f"The debit note is more than the {left:,.2f} left on {bill.number}.")


def approve(db: Session, actor: Actor, bill_id: uuid.UUID, *, reason: str = "") -> str:
    """Approve a draft (it posts, or is held if the match fails), or post a held bill with
    a reason. Returns the new status: posted or held."""
    _require(db, actor, APPROVE)
    row = _locked(db, bill_id)
    if row.status == "posted":
        raise Conflict("This has already been posted.")
    if purchasing.sod_blocked(db, actor, row.created_by):
        raise PermissionDenied(
            f"You drafted this {KIND_LABELS[row.kind].lower()}, so someone else approves it."
        )
    supplier = db.get(Supplier, row.supplier_id)
    if supplier is None or not supplier.is_active:
        raise Conflict("The supplier is inactive. Reactivate them first.")
    _price(db, row)
    if not bill_lines(db, row.id) or row.total <= 0:
        raise Conflict("Add a line with an amount first.")
    _, closed = periods.period_for(db, row.bill_date)
    if closed:
        raise Conflict(closed)
    if row.kind == "debit_note":
        _check_debit_note(db, row)
    results = match(db, row)
    failed = [r.message for r in results.values() if not r.ok]
    if failed and row.status == "draft":
        row.status, row.hold_reason = "held", "; ".join(failed)
        db.flush()
        record_by(
            db,
            actor,
            "finance.bill.hold",
            entity_type="bill",
            entity_id=str(row.id),
            changes={"status": {"from": "draft", "to": "held"}, "why": row.hold_reason},
        )
        return "held"
    if row.status == "held":
        if not _can(db, actor, OVERRIDE):
            raise PermissionDenied("A Finance Manager posts a bill that failed the match.")
        if len(reason.strip()) < MIN_REASON:
            raise ValidationFailed(field_errors={"reason": "Say why it's posted anyway."})
        row.override_reason = reason.strip()
    _post(db, actor, row, supplier)
    return "posted"


def _post(db: Session, actor: Actor, row: Bill, supplier: Supplier) -> None:
    before = row.status
    row.number = numbering.next_number(db, SEQUENCES[row.kind])
    row.status, row.approved_by, row.approved_at = "posted", actor.user_id, clock.now()
    db.flush()
    memo = f"{KIND_LABELS[row.kind]} {row.number} · {supplier.name}"
    if row.supplier_reference:
        memo += f" ({row.supplier_reference})"
    journal = journals.post_document_journal(
        db,
        source_id=row.id,
        journal_date=row.bill_date,
        memo=memo,
        lines=_journal_lines(db, row),
        poster=actor.user_id,
        source="payable",
    )
    row.journal_id = journal.id
    db.flush()
    if row.order_id:
        purchasing.close_if_billed(db, row.order_id)
    record_by(
        db,
        actor,
        f"finance.{row.kind}.post",
        entity_type="bill",
        entity_id=str(row.id),
        changes={
            "status": {"from": before, "to": "posted"},
            "number": row.number,
            "total": str(row.total),
            "override": row.override_reason,
        },
    )


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class BillLineView:
    id: uuid.UUID
    description: str
    account_id: uuid.UUID
    account: str
    cost_centre_id: uuid.UUID | None
    cost_centre: str | None
    order_line_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    net: Decimal
    vat_code: str
    vat_rate: Decimal
    vat: Decimal
    match: MatchResult | None = None

    @property
    def vat_label(self) -> str:
        if self.vat_code == "standard" and self.vat_rate:
            return f"{self.vat_rate.normalize():f}%"
        return sales.VAT_LABELS[self.vat_code]


@dataclass(frozen=True, slots=True)
class Settlement:
    kind: str  # payment or debit_note
    id: uuid.UUID
    number: str
    day: date
    amount: Decimal
    wht: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class BillView:
    id: uuid.UUID
    kind: str
    number: str | None
    status: str
    supplier_id: uuid.UUID
    supplier: str
    supplier_reference: str | None
    order: tuple[uuid.UUID, str] | None
    bill: tuple[uuid.UUID, str] | None  # a debit note's bill
    bill_date: date
    due_date: date | None
    wht_type_id: uuid.UUID | None
    wht_type: str | None
    memo: str | None
    hold_reason: str | None
    override_reason: str | None
    net: Decimal
    vat: Decimal
    total: Decimal
    paid: Decimal
    created_by: str | None
    approved_by: str | None
    approved_at: datetime | None
    journal: tuple[uuid.UUID, str] | None
    today: date
    lines: list[BillLineView] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    notes: list[tuple[uuid.UUID, str | None, str]] = field(default_factory=list)
    can_edit: bool = False
    can_approve: bool = False
    approve_blocked: str | None = None
    can_override: bool = False
    can_act: bool = False

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def shown_status(self) -> str:
        return display_status(
            self.kind, self.status, self.total, self.paid, self.due_date, self.today
        )

    @property
    def days_overdue(self) -> int:
        if self.due_date and self.balance > 0:
            return max((self.today - self.due_date).days, 0)
        return 0


def display_status(  # noqa: PLR0917  (the figures a status comes from)
    kind: str, status: str, total: Decimal, paid: Decimal, due: date | None, today: date
) -> str:
    if kind != "bill" or status != "posted":
        return status
    if paid >= total:
        return "paid"
    if due and due < today:
        return "overdue"
    return "part_paid" if paid > 0 else "posted"


def _settlements(db: Session, bill: Bill) -> list[Settlement]:
    out = [
        Settlement("payment", p.id, p.number or "", p.payment_date, line.amount, line.wht)
        for line, p in db.execute(
            select(SupplierPaymentLine, SupplierPayment)
            .join(SupplierPayment, SupplierPayment.id == SupplierPaymentLine.payment_id)
            .where(SupplierPaymentLine.bill_id == bill.id, SupplierPayment.status == "posted")
            .order_by(SupplierPayment.payment_date)
        ).all()
    ]
    out += [
        Settlement("debit_note", n.id, n.number or "", n.bill_date, n.total)
        for n in db.scalars(
            select(Bill).where(
                Bill.kind == "debit_note", Bill.status == "posted", Bill.bill_id == bill.id
            )
        )
    ]
    return sorted(out, key=lambda s: s.day)


def get_bill(db: Session, actor: Actor, bill_id: uuid.UUID, *, today: date) -> BillView:
    _require(db, actor, READ)
    row = db.get(Bill, bill_id)
    if row is None:
        raise NotFound()
    supplier = db.get(Supplier, row.supplier_id)
    order = db.get(PurchaseOrder, row.order_id) if row.order_id else None
    parent = db.get(Bill, row.bill_id) if row.bill_id else None
    wht = db.get(WhtType, row.wht_type_id) if row.wht_type_id else None
    journal = db.get(Journal, row.journal_id) if row.journal_id else None
    names = journals.user_names(db, {row.created_by, row.approved_by})
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    centres = dict(org.cost_centre_choices(db))
    results = match(db, row) if row.status != "posted" else {}
    lines = [
        BillLineView(
            line.id,
            line.description,
            line.account_id,
            codes.get(line.account_id, ""),
            line.cost_centre_id,
            centres.get(line.cost_centre_id) if line.cost_centre_id else None,
            line.order_line_id,
            line.quantity,
            line.unit_price,
            line.net,
            line.vat_code,
            line.vat_rate,
            line.vat,
            results.get(line.id),
        )
        for line in bill_lines(db, row.id)
    ]
    paid = payables.paid_amounts(db, [row.id]).get(row.id, ZERO) if row.kind == "bill" else ZERO
    open_ = row.status != "posted"
    can_create = _can(db, actor, CREATE)
    can_approve = open_ and _can(db, actor, APPROVE)
    blocked = None
    if can_approve and purchasing.sod_blocked(db, actor, row.created_by):
        can_approve = False
        blocked = f"You drafted this {KIND_LABELS[row.kind].lower()}, so someone else approves it."
    elif open_:
        _, blocked = periods.period_for(db, row.bill_date)
        can_approve = can_approve and not blocked
    notes = [
        (n.id, n.number, n.status)
        for n in db.scalars(select(Bill).where(Bill.bill_id == row.id).order_by(Bill.created_at))
    ]
    return BillView(
        id=row.id,
        kind=row.kind,
        number=row.number,
        status=row.status,
        supplier_id=row.supplier_id,
        supplier=supplier.name if supplier else "",
        supplier_reference=row.supplier_reference,
        order=(order.id, order.number or "Draft") if order else None,
        bill=(parent.id, parent.number or "") if parent else None,
        bill_date=row.bill_date,
        due_date=row.due_date,
        wht_type_id=row.wht_type_id,
        wht_type=wht.name if wht else None,
        memo=row.memo,
        hold_reason=row.hold_reason,
        override_reason=row.override_reason,
        net=row.net,
        vat=row.vat,
        total=row.total,
        paid=paid,
        created_by=names.get(row.created_by) if row.created_by else None,
        approved_by=names.get(row.approved_by) if row.approved_by else None,
        approved_at=row.approved_at,
        journal=(journal.id, journal.number) if journal and journal.number else None,
        today=today,
        lines=lines,
        settlements=_settlements(db, row) if row.kind == "bill" else [],
        notes=notes,
        can_edit=open_ and can_create,
        can_approve=can_approve,
        approve_blocked=blocked,
        can_override=row.status == "held" and can_approve and _can(db, actor, OVERRIDE),
        can_act=not open_ and can_create,
    )


@dataclass(frozen=True, slots=True)
class BillRow:
    id: uuid.UUID
    kind: str
    number: str | None
    supplier_id: uuid.UUID
    supplier: str
    supplier_reference: str | None
    order_number: str | None
    bill_date: date
    due_date: date | None
    net: Decimal
    vat: Decimal
    total: Decimal
    paid: Decimal
    status: str  # shown

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


BILL_FILTERS = {
    "bill": ("draft", "held", "posted", "part_paid", "paid", "overdue"),
    "debit_note": ("draft", "posted"),
}


def list_bills(
    db: Session,
    actor: Actor,
    kind: str,
    *,
    supplier_id: uuid.UUID | None,
    status: str,
    start: date | None,
    end: date | None,
    today: date,
) -> list[BillRow]:
    _require(db, actor, READ)
    query = (
        select(Bill, Supplier.name, PurchaseOrder.number)
        .join(Supplier, Supplier.id == Bill.supplier_id)
        .outerjoin(PurchaseOrder, PurchaseOrder.id == Bill.order_id)
        .where(Bill.kind == kind)
    )
    if supplier_id:
        query = query.where(Bill.supplier_id == supplier_id)
    if start:
        query = query.where(Bill.bill_date >= start)
    if end:
        query = query.where(Bill.bill_date <= end)
    rows = db.execute(query.order_by(Bill.bill_date.desc(), Bill.number.desc()).limit(500)).all()
    paid = payables.paid_amounts(db, [b.id for b, _, _ in rows]) if kind == "bill" else {}
    out = []
    for b, name, order_number in rows:
        settled = paid.get(b.id, ZERO)
        shown = display_status(kind, b.status, b.total, settled, b.due_date, today)
        if status and shown != status:
            continue
        out.append(
            BillRow(
                b.id,
                b.kind,
                b.number,
                b.supplier_id,
                name,
                b.supplier_reference,
                order_number,
                b.bill_date,
                b.due_date,
                b.net,
                b.vat,
                b.total,
                settled,
                shown,
            )
        )
    return out


def wht_amount(settled: Decimal, net: Decimal, total: Decimal, rate: Decimal) -> Decimal:
    """WHT on the part of an amount settled that isn't VAT (ADR 0025)."""
    if not total or not rate:
        return ZERO
    base = settled * net / total
    return (base * rate / HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
