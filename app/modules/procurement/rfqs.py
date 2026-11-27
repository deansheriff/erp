"""Requests for quotation (ADR 0030). A Procurement Officer invites suppliers to quote on
an approved requisition and emails each the RFQ as a PDF, then records the quotes that
come back. Over the quotes threshold, three quotes are needed before an award. Choosing a
quote that isn't the lowest net total needs a reason. If the award's net total needs a
higher approval tier than the requisition had, it goes back for the missing approvers;
once it's approved for the amount, the officer makes a draft purchase order that someone
else approves in Payables."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.mail import service as mail
from app.mail.transport import Attachment
from app.modules.finance import service as finance
from app.modules.inventory import service as inventory
from app.modules.procurement import pdf, requisitions
from app.modules.procurement import settings as setup
from app.modules.procurement.models import (
    Quote,
    QuoteLine,
    Requisition,
    RequisitionLine,
    Rfq,
    RfqSupplier,
)

READ = "procurement.rfq.read"
MANAGE = "procurement.rfq.manage"
ZERO = Decimal(0)
CENT = Decimal("0.01")
MIN_REASON = 5
STATUS_LABELS = {
    "draft": "Draft",
    "sent": "Sent",
    "awarded": "Awarded",
    "ordered": "Ordered",
    "cancelled": "Cancelled",
}
STATUS_TONES = {
    "draft": "neutral",
    "sent": "info",
    "awarded": "warning",
    "ordered": "success",
    "cancelled": "neutral",
}


def _rfq(db: Session, rfq_id: uuid.UUID, *, lock: bool = False) -> Rfq:
    query = select(Rfq).where(Rfq.id == rfq_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def _invited(db: Session, rfq_id: uuid.UUID) -> list[RfqSupplier]:
    return list(
        db.scalars(select(RfqSupplier).where(RfqSupplier.rfq_id == rfq_id).order_by(RfqSupplier.id))
    )


def quotes_of(db: Session, rfq_id: uuid.UUID) -> list[Quote]:
    return list(db.scalars(select(Quote).where(Quote.rfq_id == rfq_id).order_by(Quote.net)))


def _quote_lines(db: Session, quote_id: uuid.UUID) -> dict[uuid.UUID, QuoteLine]:
    return {
        q.line_id: q for q in db.scalars(select(QuoteLine).where(QuoteLine.quote_id == quote_id))
    }


def _open(row: Rfq) -> None:
    if row.status not in ("draft", "sent"):
        raise Conflict("This RFQ has been awarded or cancelled, so it can't be changed.")


def _audit(db: Session, actor: Actor, action: str, row: Rfq, **changes: object) -> None:
    record_by(
        db,
        actor,
        f"procurement.rfq.{action}",
        entity_type="rfq",
        entity_id=str(row.id),
        changes=dict(changes) or None,
    )


# ------------------------------------------------------------------ starting and inviting


def _active_supplier(db: Session, supplier_id: str) -> uuid.UUID | None:
    sid = setup.uuid_or_none(supplier_id)
    found = finance.suppliers_by_id(db, {sid}) if sid else {}
    return sid if sid and sid in found and found[sid].is_active else None


def start(
    db: Session,
    actor: Actor,
    requisition_id: uuid.UUID,
    *,
    respond_by: date | None,
    note: str,
    supplier_ids: list[str],
) -> uuid.UUID:
    setup.require(db, actor, MANAGE)
    req = requisitions.row_of(db, requisition_id, lock=True)
    if req.status != "approved":
        raise Conflict("Only an approved requisition can go out for quotes.")
    if requisitions.live_rfq(db, req.id) is not None:
        raise Conflict("There's already an RFQ for this requisition.")
    errors: dict[str, str] = {}
    if respond_by and respond_by < clock.today():
        errors["respond_by"] = "Choose today or later."
    chosen = list(dict.fromkeys(s for s in (_active_supplier(db, x) for x in supplier_ids) if s))
    if not chosen:
        errors["supplier_ids"] = "Invite at least one active supplier."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = Rfq(
        number=numbering.next_number(db, "rfq"),
        requisition_id=req.id,
        respond_by=respond_by,
        note=note.strip() or None,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for sid in chosen:
        db.add(RfqSupplier(rfq_id=row.id, supplier_id=sid))
    db.flush()
    _audit(db, actor, "start", row, number=row.number, suppliers=len(chosen))
    return row.id


def invite(db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: str) -> None:
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    sid = _active_supplier(db, supplier_id)
    if sid is None:
        raise ValidationFailed(field_errors={"supplier_id": "Choose an active supplier."})
    if any(s.supplier_id == sid for s in _invited(db, row.id)):
        raise ValidationFailed(field_errors={"supplier_id": "That supplier is already invited."})
    db.add(RfqSupplier(rfq_id=row.id, supplier_id=sid))
    db.flush()
    _audit(db, actor, "invite", row, supplier=str(sid))


def remove(db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID) -> None:
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    entry = next((s for s in _invited(db, row.id) if s.supplier_id == supplier_id), None)
    if entry is None:
        raise NotFound()
    if any(q.supplier_id == supplier_id for q in quotes_of(db, row.id)):
        raise Conflict("This supplier has quoted. Mark them as declined instead.")
    db.delete(entry)
    db.flush()
    _audit(db, actor, "remove", row, supplier=str(supplier_id))


# ------------------------------------------------------------------ the PDF and email


def _document(db: Session, row: Rfq, supplier: finance.SupplierInfo) -> pdf.RfqDocument:
    lines = requisitions.line_views(db, requisitions.lines_of(db, row.requisition_id))
    units = _units(db, lines)
    return pdf.RfqDocument(
        finance.organisation_name(db),
        row.number,
        (row.sent_at or clock.now()).date(),
        row.respond_by,
        supplier.name,
        supplier.address,
        row.note,
        [
            pdf.RfqLine(x.description, f"{x.quantity.normalize():f}", units.get(x.id, ""))
            for x in lines
        ],
    )


def _units(db: Session, lines: list[requisitions.LineView]) -> dict[uuid.UUID, str]:
    out = {}
    for line in lines:
        if line.item_id:
            item = inventory.LINK.item(db, line.item_id)
            out[line.id] = item.unit if item else ""
    return out


def pdf_for(
    db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID | None
) -> tuple[str, bytes]:
    """The RFQ as a supplier receives it (any invited supplier's, or a blank one)."""
    setup.require(db, actor, READ)
    row = _rfq(db, rfq_id)
    invited = [s.supplier_id for s in _invited(db, row.id)]
    sid = supplier_id if supplier_id in invited else None
    supplier = finance.suppliers_by_id(db, {sid}).get(sid) if sid else None
    blank = finance.SupplierInfo(uuid.uuid4(), "", "Supplier", None, None, True)
    return f"{row.number}.pdf", pdf.draw(_document(db, row, supplier or blank))


@dataclass(frozen=True, slots=True)
class Sent:
    emailed: int
    without_email: list[str]


def send(db: Session, actor: Actor, rfq_id: uuid.UUID) -> Sent:
    """Email the RFQ to each invited supplier not yet emailed, with the PDF attached."""
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    invited = _invited(db, row.id)
    if not invited:
        raise Conflict("Invite at least one supplier first.")
    row.status, row.sent_at = "sent", row.sent_at or clock.now()
    suppliers = finance.suppliers_by_id(db, {s.supplier_id for s in invited})
    lines = requisitions.line_views(db, requisitions.lines_of(db, row.requisition_id))
    units = _units(db, lines)
    emailed, missing = 0, []
    for entry in invited:
        supplier = suppliers[entry.supplier_id]
        if entry.emailed_at or entry.declined:
            continue
        if not supplier.email:
            missing.append(supplier.name)
            continue
        mail.queue(
            db,
            to=supplier.email,
            template="procurement_rfq",
            context={
                "organisation": finance.organisation_name(db),
                "supplier": supplier.name,
                "number": row.number,
                "respond_by": f"{row.respond_by:%d %b %Y}" if row.respond_by else "",
                "note": row.note or "",
                "lines": [
                    {
                        "description": x.description,
                        "quantity": f"{x.quantity.normalize():f}",
                        "unit": units.get(x.id, ""),
                    }
                    for x in lines
                ],
            },
            attachment=Attachment(
                f"{row.number}.pdf", "application/pdf", pdf.draw(_document(db, row, supplier))
            ),
        )
        entry.emailed_at = clock.now()
        emailed += 1
    db.flush()
    _audit(db, actor, "send", row, emailed=emailed, without_email=len(missing))
    return Sent(emailed, missing)


# ------------------------------------------------------------------ quotes


@dataclass(frozen=True, slots=True)
class QuoteInput:
    reference: str
    received_on: date | None
    valid_until: date | None
    delivery_days: str
    note: str
    prices: dict[str, str]  # by requisition line id
    vat_codes: dict[str, str]


def _days(text: str, errors: dict[str, str]) -> int | None:
    if not text.strip():
        return None
    try:
        days = int(text.strip())
    except ValueError:
        days = -1
    if not 0 <= days <= 365:
        errors["delivery_days"] = "Enter the delivery time in days, like 7."
        return None
    return days


def _priced(
    db: Session, data: QuoteInput, lines: list[RequisitionLine], errors: dict[str, str]
) -> list[tuple[RequisitionLine, Decimal, str]]:
    out = []
    for line in lines:
        key = f"price_{line.id}"
        text = data.prices.get(str(line.id), "")
        if not text.strip():
            errors[key] = "Enter the supplier's unit price."
            continue
        price = setup.parse_money(text, key, errors, label="a unit price")
        code = data.vat_codes.get(str(line.id), "standard")
        if code not in finance.VAT_CODES:
            errors[key] = "Choose the VAT that applies."
        if key not in errors:
            out.append((line, price, code))
    return out


def _check_quote(row: Rfq, data: QuoteInput, errors: dict[str, str]) -> None:
    if data.received_on is None:
        errors["received_on"] = "Enter the date the quote arrived."
    elif data.received_on > clock.today():
        errors["received_on"] = "The quote can't arrive in the future."
    if data.valid_until and data.received_on and data.valid_until < data.received_on:
        errors["valid_until"] = "It can't expire before it arrived."
    del row


def record_quote(
    db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID, data: QuoteInput
) -> uuid.UUID:
    """Record (or correct) an invited supplier's quote. Every line needs a price."""
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    entry = next((s for s in _invited(db, row.id) if s.supplier_id == supplier_id), None)
    if entry is None:
        raise NotFound()
    errors: dict[str, str] = {}
    _check_quote(row, data, errors)
    days = _days(data.delivery_days, errors)
    priced = _priced(db, data, requisitions.lines_of(db, row.requisition_id), errors)
    rate = finance.vat_rate_on(db, data.received_on) if data.received_on else None
    if not errors and rate is None and any(code == "standard" for _, _, code in priced):
        errors["form"] = "No VAT rate is set for that date. Add one in Tax settings."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.received_on is not None  # noqa: S101  (checked above)
    quote = next((q for q in quotes_of(db, row.id) if q.supplier_id == supplier_id), None)
    if quote is None:
        quote = Quote(rfq_id=row.id, supplier_id=supplier_id, received_on=data.received_on)
        db.add(quote)
        db.flush()
    quote.reference = data.reference.strip()[:80] or None
    quote.received_on, quote.valid_until = data.received_on, data.valid_until
    quote.delivery_days, quote.note = days, data.note.strip() or None
    quote.created_by = actor.user_id
    for old in _quote_lines(db, quote.id).values():
        db.delete(old)
    db.flush()
    net = vat = ZERO
    for line, price, code in priced:
        line_net = (line.quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)
        line_rate = rate if code == "standard" and rate is not None else ZERO
        line_vat = finance.vat_amount(line_net, line_rate) if line_rate else ZERO
        db.add(
            QuoteLine(
                quote_id=quote.id,
                line_id=line.id,
                unit_price=price,
                vat_code=code,
                vat_rate=line_rate,
                net=line_net,
                vat=line_vat,
            )
        )
        net, vat = net + line_net, vat + line_vat
    quote.net, quote.vat, quote.total = net, vat, net + vat
    entry.declined = False
    db.flush()
    _audit(db, actor, "quote", row, supplier=str(supplier_id), net=str(net))
    return quote.id


def attach(
    db: Session,
    actor: Actor,
    quote_id: uuid.UUID,
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
) -> None:
    """Keep the supplier's own document with the quote."""
    setup.require(db, actor, MANAGE)
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise NotFound()
    quote.file_id = files.upload(
        db,
        actor,
        purpose_key="supplier_quote",
        entity_id=str(quote.id),
        filename=filename,
        declared_mime=content_type,
        data=data,
        replaces=quote.file_id,
    )
    db.flush()


def decline(db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID) -> None:
    """The supplier won't quote: drop any quote recorded for them."""
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    entry = next((s for s in _invited(db, row.id) if s.supplier_id == supplier_id), None)
    if entry is None:
        raise NotFound()
    for quote in quotes_of(db, row.id):
        if quote.supplier_id == supplier_id:
            db.delete(quote)
    entry.declined = True
    db.flush()
    _audit(db, actor, "decline", row, supplier=str(supplier_id))


# ------------------------------------------------------------------ award and order


def award(db: Session, actor: Actor, rfq_id: uuid.UUID, quote_id: str, *, reason: str) -> str:
    """Award a quote. Returns "awarded", or "approval" when the requisition must first be
    approved for the higher amount."""
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    _open(row)
    req = requisitions.row_of(db, row.requisition_id, lock=True)
    quotes = quotes_of(db, row.id)
    chosen = next((q for q in quotes if str(q.id) == quote_id), None)
    if chosen is None:
        raise ValidationFailed(field_errors={"quote_id": "Choose the quote to award."})
    needed = setup.quotes_needed(db, req.estimated_total)
    if len(quotes) < needed:
        raise Conflict(
            f"This requisition is over {setup.settings(db).quote_threshold:,.2f}, so it needs "
            f"at least {needed} quotes before an award. {len(quotes)} recorded so far."
        )
    lowest = min(q.net for q in quotes)
    if chosen.net > lowest and len(reason.strip()) < MIN_REASON:
        raise ValidationFailed(field_errors={"reason": "Say why it isn't the lowest quote."})
    chosen.awarded = True
    row.status = "awarded"
    row.award_reason = reason.strip() or None
    row.awarded_by, row.awarded_at = actor.user_id, clock.now()
    db.flush()
    added = requisitions.require_for(db, req, chosen.net)
    _audit(
        db,
        actor,
        "award",
        row,
        quote=str(chosen.id),
        net=str(chosen.net),
        lowest=chosen.net == lowest,
        needs=added,
    )
    return "approval" if added else "awarded"


def _order_lines(
    db: Session,
    lines: list[RequisitionLine],
    priced: dict[uuid.UUID, QuoteLine],
    accounts: dict[str, str],
) -> list[finance.OrderLineDraft]:
    allowed = {a.id for a in finance.purchase_accounts(db)}
    errors: dict[str, str] = {}
    out = []
    for line in lines:
        account = line.account_id or setup.uuid_or_none(accounts.get(str(line.id), ""))
        if line.item_id is None and account not in allowed:
            errors[f"account_{line.id}"] = "Choose the account to charge."
            continue
        quoted = priced[line.id]
        out.append(
            finance.OrderLineDraft(
                line.description,
                None if line.item_id else account,
                line.quantity,
                quoted.unit_price,
                quoted.vat_code,
                line.item_id,
                line.warehouse_id,
            )
        )
    if errors:
        raise ValidationFailed(field_errors=errors)
    return out


def make_order(
    db: Session, actor: Actor, rfq_id: uuid.UUID, *, accounts: dict[str, str]
) -> uuid.UUID:
    """Draft the purchase order from the awarded quote. Returns the order's id."""
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    if row.status != "awarded":
        raise Conflict("Award a quote first.")
    req = requisitions.row_of(db, row.requisition_id, lock=True)
    if req.status != "approved":
        raise Conflict("The requisition must be approved for the quoted amount first.")
    quote = next(q for q in quotes_of(db, row.id) if q.awarded)
    lines = requisitions.lines_of(db, req.id)
    drafts = _order_lines(db, lines, _quote_lines(db, quote.id), accounts)
    today = clock.today()
    order_id = finance.draft_order(
        db,
        actor,
        supplier_id=quote.supplier_id,
        order_date=today,
        expected_date=today + timedelta(days=quote.delivery_days)
        if quote.delivery_days is not None
        else None,
        reference=(quote.reference or row.number)[:80],
        memo=f"From {req.number} via {row.number}"[:200],
        cost_centre_id=req.cost_centre_id,
        lines=drafts,
    )
    row.order_id, row.status = order_id, "ordered"
    req.status = "ordered"
    db.flush()
    _audit(db, actor, "order", row, order=str(order_id))
    return order_id


def cancel(db: Session, actor: Actor, rfq_id: uuid.UUID) -> None:
    setup.require(db, actor, MANAGE)
    row = _rfq(db, rfq_id, lock=True)
    if row.status in ("ordered", "cancelled"):
        raise Conflict("An ordered or cancelled RFQ can't be cancelled.")
    for quote in quotes_of(db, row.id):
        quote.awarded = False
    row.status = "cancelled"
    db.flush()
    _audit(db, actor, "cancel", row)


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class RfqRow:
    id: uuid.UUID
    number: str
    requisition_id: uuid.UUID
    requisition: str
    purpose: str
    invited: int
    quotes: int
    needed: int
    respond_by: date | None
    lowest: Decimal | None
    status: str


def list_rfqs(db: Session, actor: Actor, tab: str) -> list[RfqRow]:
    setup.require(db, actor, READ)
    query = select(Rfq, Requisition).join(Requisition, Requisition.id == Rfq.requisition_id)
    wanted = {"open": ("draft", "sent"), "awarded": ("awarded",), "ordered": ("ordered",)}
    if tab in wanted:
        query = query.where(Rfq.status.in_(wanted[tab]))
    out = []
    for row, req in db.execute(query.order_by(Rfq.created_at.desc()).limit(300)).all():
        quotes = quotes_of(db, row.id)
        out.append(
            RfqRow(
                row.id,
                row.number,
                req.id,
                req.number or "",
                req.purpose,
                len(_invited(db, row.id)),
                len(quotes),
                setup.quotes_needed(db, req.estimated_total),
                row.respond_by,
                min((q.net for q in quotes), default=None),
                row.status,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class QuoteView:
    id: uuid.UUID
    supplier_id: uuid.UUID
    reference: str | None
    received_on: date
    valid_until: date | None
    delivery_days: int | None
    note: str | None
    file_id: uuid.UUID | None
    net: Decimal
    vat: Decimal
    total: Decimal
    awarded: bool
    lowest: bool
    prices: dict[uuid.UUID, QuoteLine]


@dataclass(frozen=True, slots=True)
class SupplierEntry:
    supplier_id: uuid.UUID
    name: str
    email: str | None
    emailed_at: datetime | None
    declined: bool
    quote: QuoteView | None


@dataclass(frozen=True, slots=True)
class RfqView:
    row: Rfq
    requisition: Requisition
    lines: list[requisitions.LineView]
    units: dict[uuid.UUID, str]
    suppliers: list[SupplierEntry]
    quotes: list[QuoteView]  # lowest net first
    needed: int
    threshold: Decimal
    cheapest: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # line -> quote
    awarded: QuoteView | None = None
    awarded_by: str | None = None
    waiting_for: list[str] = field(default_factory=list)  # approvals the award needs
    order: tuple[uuid.UUID, str, str] | None = None  # id, number, status
    can_manage: bool = False

    @property
    def is_open(self) -> bool:
        return self.row.status in ("draft", "sent")


def _quote_views(db: Session, quotes: list[Quote]) -> list[QuoteView]:
    lowest = min((q.net for q in quotes), default=None)
    return [
        QuoteView(
            q.id,
            q.supplier_id,
            q.reference,
            q.received_on,
            q.valid_until,
            q.delivery_days,
            q.note,
            q.file_id,
            q.net,
            q.vat,
            q.total,
            q.awarded,
            q.net == lowest,
            _quote_lines(db, q.id),
        )
        for q in quotes
    ]


def _cheapest(quotes: list[QuoteView]) -> dict[uuid.UUID, uuid.UUID]:
    best: dict[uuid.UUID, tuple[Decimal, uuid.UUID]] = {}
    for quote in quotes:
        for line_id, priced in quote.prices.items():
            if line_id not in best or priced.unit_price < best[line_id][0]:
                best[line_id] = (priced.unit_price, quote.id)
    return {line_id: quote_id for line_id, (_, quote_id) in best.items()}


def get(db: Session, actor: Actor, rfq_id: uuid.UUID) -> RfqView:
    setup.require(db, actor, READ)
    row = _rfq(db, rfq_id)
    req = requisitions.row_of(db, row.requisition_id)
    lines = requisitions.line_views(db, requisitions.lines_of(db, req.id))
    quotes = _quote_views(db, quotes_of(db, row.id))
    by_supplier = {q.supplier_id: q for q in quotes}
    invited = _invited(db, row.id)
    info = finance.suppliers_by_id(db, {s.supplier_id for s in invited})
    awarded = next((q for q in quotes if q.awarded), None)
    waiting = []
    if awarded and req.status == "submitted":
        waiting = [
            setup.LEVEL_LABELS[s.level]
            for s in requisitions.steps_of(db, req.id)
            if s.status == "pending"
        ]
    order = None
    if row.order_id:
        number, status = finance.order_status(db, row.order_id)
        order = (row.order_id, number, status)
    return RfqView(
        row,
        req,
        lines,
        _units(db, lines),
        [
            SupplierEntry(
                s.supplier_id,
                info[s.supplier_id].name,
                info[s.supplier_id].email,
                s.emailed_at,
                s.declined,
                by_supplier.get(s.supplier_id),
            )
            for s in invited
        ],
        quotes,
        setup.quotes_needed(db, req.estimated_total),
        setup.settings(db).quote_threshold,
        _cheapest(quotes),
        awarded,
        setup.user_names(db, {row.awarded_by}).get(row.awarded_by) if row.awarded_by else None,
        waiting,
        order,
        setup.can(db, actor, MANAGE),
    )


def quote_form(
    db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID
) -> tuple[RfqView, SupplierEntry]:
    view = get(db, actor, rfq_id)
    entry = next((s for s in view.suppliers if s.supplier_id == supplier_id), None)
    if entry is None:
        raise NotFound()
    return view, entry


def supplier_quotes(db: Session, supplier_id: uuid.UUID) -> list[tuple[Rfq, Quote]]:
    """The supplier's quotes, newest first, with their RFQs (for performance)."""
    return [
        (r, q)
        for q, r in db.execute(
            select(Quote, Rfq)
            .join(Rfq, Rfq.id == Quote.rfq_id)
            .where(Quote.supplier_id == supplier_id)
            .order_by(Quote.received_on.desc())
        ).all()
    ]


def rfq_of_quote(db: Session, quote_id: uuid.UUID) -> uuid.UUID:
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise NotFound()
    return quote.rfq_id
