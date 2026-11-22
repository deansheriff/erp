"""Quotes, sales orders, invoices and credit notes (ADR 0024). Every step is optional;
converting copies the lines into a new draft. Issuing an invoice or credit note posts its
journal in the same transaction; a credit note is issued by someone other than its
drafter. Issued documents are never edited."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.mail import service as mail
from app.modules.finance import accounts, journals, pdf, periods, receivables, tax
from app.modules.finance.models import (
    DOCUMENT_KINDS,
    VAT_CODES,
    Account,
    Allocation,
    Customer,
    Journal,
    Receipt,
    SalesDocument,
    SalesLine,
    StampDutyType,
)
from app.org import service as org

READ = "finance.sales.read"
CREATE = "finance.sales.create"
ISSUE_CREDIT = "finance.credit_note.issue"
CENT = Decimal("0.01")
ZERO = Decimal(0)
QTY = Decimal("0.001")
KIND_LABELS = {
    "quote": "Quote",
    "order": "Sales order",
    "invoice": "Invoice",
    "credit_note": "Credit note",
}
SEQUENCES = {
    "quote": "quote",
    "order": "sales_order",
    "invoice": "invoice",
    "credit_note": "credit_note",
}
ISSUED_STATUS = {
    "quote": "sent",
    "order": "confirmed",
    "invoice": "issued",
    "credit_note": "issued",
}
VAT_LABELS = {"standard": "Standard", "zero": "Zero-rated", "exempt": "Exempt"}
STATUS_LABELS = {
    "draft": "Draft",
    "sent": "Sent",
    "accepted": "Accepted",
    "declined": "Declined",
    "converted": "Converted",
    "confirmed": "Confirmed",
    "invoiced": "Invoiced",
    "cancelled": "Cancelled",
    "issued": "Issued",
    "part_paid": "Part paid",
    "paid": "Paid",
    "overdue": "Overdue",
    "credited": "Fully credited",
}
STATUS_TONES = {
    "draft": "neutral",
    "sent": "info",
    "accepted": "success",
    "declined": "neutral",
    "converted": "neutral",
    "confirmed": "info",
    "invoiced": "neutral",
    "cancelled": "neutral",
    "issued": "info",
    "part_paid": "warning",
    "paid": "success",
    "overdue": "danger",
    "credited": "neutral",
}
# What each kind may become, and the status the source takes.
CONVERSIONS = {"quote": ("order", "invoice"), "order": ("invoice",)}
CONVERTED_STATUS = {"quote": "converted", "order": "invoiced"}
RESTORED_STATUS = {"quote": "accepted", "order": "confirmed"}
CONVERTIBLE_FROM = {"quote": ("sent", "accepted"), "order": ("confirmed",)}


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
class LineInput:
    description: str
    account_id: str
    cost_centre_id: str
    quantity: str
    unit_price: str
    vat_code: str


@dataclass(frozen=True, slots=True)
class DocumentInput:
    kind: str
    customer_id: str
    doc_date: date | None
    due_date: date | None
    valid_until: date | None
    reference: str
    memo: str
    stamp_duty_type_id: str
    lines: list[LineInput]


@dataclass(frozen=True, slots=True)
class ParsedLine:
    description: str
    account: Account
    cost_centre_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    vat_code: str

    @property
    def net(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)


def income_accounts(db: Session) -> list[Account]:
    return [a for a in accounts.postable(db) if a.type == "income"]


def _quantity(value: str, key: str, errors: dict[str, str]) -> Decimal:
    text = value.strip().replace(",", "") or "1"
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = ZERO
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(QTY):
        errors[key] = "Enter a quantity above 0, to three decimal places at most."
    return amount


def parse_lines(
    db: Session, lines: list[LineInput], *, allowed: list[Account] | None = None
) -> tuple[list[ParsedLine], dict[str, str]]:
    """Form lines, skipping empty rows. Errors are keyed `line_<n>` by the row's place.
    Accounts are income ones unless `allowed` says otherwise (purchases use expenses)."""
    errors: dict[str, str] = {}
    known = {a.id: a for a in (allowed if allowed is not None else income_accounts(db))}
    wrong_account = "Choose an income account." if allowed is None else "Choose an account."
    centres = {cid for cid, _ in org.cost_centre_choices(db)}
    parsed = []
    for i, line in enumerate(lines):
        if not (line.description.strip() or line.account_id or line.unit_price.strip()):
            continue
        key = f"line_{i}"
        quantity = _quantity(line.quantity, key, errors)
        price = journals.parse_amount(line.unit_price, key, errors)
        if key in errors:
            continue
        account = known.get(_uuid(line.account_id))  # type: ignore[arg-type]
        centre = _uuid(line.cost_centre_id)
        if not line.description.strip():
            errors[key] = "Describe the line."
        elif account is None:
            errors[key] = wrong_account
        elif line.vat_code not in VAT_CODES:
            errors[key] = "Choose standard, zero-rated or exempt."
        elif centre is not None and centre not in centres:
            errors[key] = "Choose an active cost centre."
        else:
            parsed.append(
                ParsedLine(
                    line.description.strip()[:300], account, centre, quantity, price, line.vat_code
                )
            )
    return parsed, errors


# ------------------------------------------------------------------ drafts


def _locked(db: Session, document_id: uuid.UUID) -> SalesDocument:
    row = db.scalars(
        select(SalesDocument).where(SalesDocument.id == document_id).with_for_update()
    ).first()
    if row is None:
        raise NotFound()
    return row


def _lines(db: Session, document_id: uuid.UUID) -> list[SalesLine]:
    return list(
        db.scalars(
            select(SalesLine)
            .where(SalesLine.document_id == document_id)
            .order_by(SalesLine.position)
        )
    )


def _stamp(db: Session, stamp_id: uuid.UUID | None) -> StampDutyType | None:
    return db.get(StampDutyType, stamp_id) if stamp_id else None


def _price(db: Session, doc: SalesDocument) -> None:
    """Work out each line's VAT at the rate in force on the document date, then the totals.
    Raises Conflict when a standard-rated line has no rate to use."""
    rate = tax.vat_rate_on(db, doc.doc_date)
    net = vat = ZERO
    for line in _lines(db, doc.id):
        line.net = (line.quantity * line.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        if line.vat_code == "standard":
            if rate is None:
                raise Conflict("No VAT rate is set for this date. Add one in Tax settings.")
            line.vat_rate, line.vat = rate, tax.vat_amount(line.net, rate)
        else:
            line.vat_rate, line.vat = ZERO, ZERO
        net += line.net
        vat += line.vat
    duty = _stamp(db, doc.stamp_duty_type_id)
    doc.net, doc.vat = net, vat
    doc.stamp_duty = tax.stamp_amount(duty, net) if duty and doc.kind != "quote" else ZERO
    doc.total = net + vat + doc.stamp_duty
    db.flush()


def _check_header(db: Session, data: DocumentInput, errors: dict[str, str]) -> Customer | None:
    if data.kind not in DOCUMENT_KINDS:
        raise ValidationFailed(field_errors={"form": "Choose what to raise."})
    customer = db.get(Customer, _uuid(data.customer_id)) if _uuid(data.customer_id) else None
    if customer is None or not customer.is_active:
        errors["customer_id"] = "Choose an active customer."
    if data.doc_date is None:
        errors["doc_date"] = "Enter the date."
    if data.due_date and data.doc_date and data.due_date < data.doc_date:
        errors["due_date"] = "The due date can't be before the invoice date."
    if data.valid_until and data.doc_date and data.valid_until < data.doc_date:
        errors["valid_until"] = "A quote can't expire before its date."
    stamp_id = _uuid(data.stamp_duty_type_id)
    if stamp_id:
        duty = _stamp(db, stamp_id)
        if duty is None or not duty.is_active:
            errors["stamp_duty_type_id"] = "Choose an active stamp duty type."
    return customer


def save_draft(
    db: Session, actor: Actor, document_id: uuid.UUID | None, data: DocumentInput
) -> uuid.UUID:
    """Create or change a draft. Its numbers are worked out again when it is issued."""
    _require(db, actor, CREATE)
    row = _locked(db, document_id) if document_id else None
    if row is not None and row.status != "draft":
        raise Conflict("Only a draft can be changed.")
    if row is not None and row.kind != data.kind:
        raise Conflict("A draft keeps its kind.")
    errors: dict[str, str] = {}
    customer = _check_header(db, data, errors)
    parsed, line_errors = parse_lines(db, data.lines)
    errors.update(line_errors)
    if not parsed and not errors:
        errors["form"] = "Add at least one line."
    if row is not None and row.invoice_id and customer and row.customer_id != customer.id:
        errors["customer_id"] = "A credit note stays with its invoice's customer."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert customer is not None and data.doc_date is not None  # noqa: S101  (checked above)
    if row is None:
        row = SalesDocument(
            kind=data.kind, status="draft", customer_id=customer.id, doc_date=data.doc_date
        )
        db.add(row)
    row.customer_id, row.doc_date = customer.id, data.doc_date
    row.due_date = (
        (data.due_date or data.doc_date + timedelta(days=customer.payment_terms_days))
        if data.kind == "invoice"
        else None
    )
    row.valid_until = data.valid_until if data.kind == "quote" else None
    row.reference = data.reference.strip()[:80] or None
    row.memo = data.memo.strip()[:200] or None
    row.stamp_duty_type_id = _uuid(data.stamp_duty_type_id) if data.kind != "quote" else None
    row.created_by = actor.user_id  # whoever last drafted a credit note can't issue it
    db.flush()
    _replace_lines(db, row, parsed)
    try:
        _price(db, row)
    except Conflict:  # no VAT rate yet: keep the draft, issuing will say so
        db.flush()
    record_by(
        db,
        actor,
        f"finance.{row.kind}.draft",
        entity_type="sales_document",
        entity_id=str(row.id),
        changes={"lines": {"from": None, "to": len(parsed)}},
    )
    return row.id


def _replace_lines(db: Session, doc: SalesDocument, parsed: list[ParsedLine]) -> None:
    for old in _lines(db, doc.id):
        db.delete(old)
    db.flush()
    for position, line in enumerate(parsed, start=1):
        db.add(
            SalesLine(
                document_id=doc.id,
                position=position,
                description=line.description,
                account_id=line.account.id,
                cost_centre_id=line.cost_centre_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                net=line.net,
                vat_code=line.vat_code,
                vat_rate=ZERO,
                vat=ZERO,
            )
        )
    db.flush()


def delete_draft(db: Session, actor: Actor, document_id: uuid.UUID) -> str:
    """Delete a draft; a quote or order it was converted from goes back. Returns its kind."""
    _require(db, actor, CREATE)
    row = _locked(db, document_id)
    if row.status != "draft":
        raise Conflict("Only a draft can be deleted.")
    if row.source_document_id:
        source = _locked(db, row.source_document_id)
        if source.status == CONVERTED_STATUS.get(source.kind):
            source.status = RESTORED_STATUS[source.kind]
    kind = row.kind
    record_by(
        db, actor, f"finance.{kind}.delete", entity_type="sales_document", entity_id=str(row.id)
    )
    db.delete(row)
    db.flush()
    return kind


# ------------------------------------------------------------------ issuing


def _sod_blocked(db: Session, actor: Actor, doc: SalesDocument) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), ISSUE_CREDIT, actor.user_id, {"created_by": doc.created_by}, held
        )
    )


def _journal_lines(db: Session, doc: SalesDocument) -> list[journals.SystemLine]:
    """Invoice: debit receivables, credit income, VAT and stamp duty. A credit note is the
    same the other way round."""
    settings = periods.settings(db)
    codes = {a.id: a.code for a in accounts.all_accounts(db)}
    sides: list[tuple[str, uuid.UUID | None, Decimal]] = [
        (codes[line.account_id], line.cost_centre_id, line.net) for line in _lines(db, doc.id)
    ]
    sides.append((settings.vat_output_code, None, doc.vat))
    sides.append((settings.stamp_duty_code, None, doc.stamp_duty))
    credit_note = doc.kind == "credit_note"
    out: list[journals.SystemLine] = [
        (settings.receivables_code, None, ZERO, doc.total)
        if credit_note
        else (settings.receivables_code, None, doc.total, ZERO)
    ]
    for code, centre, amount in sides:
        if amount:
            out.append(
                (code, centre, amount, ZERO) if credit_note else (code, centre, ZERO, amount)
            )
    return out


def issue(db: Session, actor: Actor, document_id: uuid.UUID) -> str:
    """Send a quote, confirm an order, or issue an invoice or credit note. Returns its number."""
    doc = _locked(db, document_id)
    if doc.kind == "credit_note":
        _require(db, actor, ISSUE_CREDIT)
        if _sod_blocked(db, actor, doc):
            raise PermissionDenied("You drafted this credit note, so someone else issues it.")
    else:
        _require(db, actor, CREATE)
    if doc.status != "draft":
        raise Conflict("This has already been issued.")
    customer = db.get(Customer, doc.customer_id)
    if customer is None or not customer.is_active:
        raise Conflict("The customer is inactive. Reactivate them first.")
    _price(db, doc)
    if not _lines(db, doc.id) or doc.total <= 0:
        raise Conflict("Add a line with an amount first.")
    if doc.kind == "credit_note" and doc.invoice_id:
        _check_against_invoice(db, doc)
    if doc.kind in ("invoice", "credit_note"):
        _, closed = periods.period_for(db, doc.doc_date)
        if closed:
            raise Conflict(closed)
    doc.number = numbering.next_number(db, SEQUENCES[doc.kind])
    doc.status = ISSUED_STATUS[doc.kind]
    doc.issued_by, doc.issued_at = actor.user_id, clock.now()
    db.flush()
    if doc.kind in ("invoice", "credit_note"):
        memo = f"{KIND_LABELS[doc.kind]} {doc.number} · {customer.name}"
        journal = journals.post_document_journal(
            db,
            source_id=doc.id,
            journal_date=doc.doc_date,
            memo=memo,
            lines=_journal_lines(db, doc),
            poster=actor.user_id,
        )
        doc.journal_id = journal.id
        db.flush()
    if doc.kind == "credit_note" and doc.invoice_id:
        left = _invoice_balance(db, doc.invoice_id)
        amount = min(left, doc.total)
        if amount > 0:
            db.add(
                Allocation(
                    invoice_id=doc.invoice_id,
                    credit_note_id=doc.id,
                    amount=amount,
                    allocated_on=doc.doc_date,
                    created_by=actor.user_id,
                )
            )
            db.flush()
    record_by(
        db,
        actor,
        f"finance.{doc.kind}.issue",
        entity_type="sales_document",
        entity_id=str(doc.id),
        changes={"number": {"from": None, "to": doc.number}, "total": str(doc.total)},
    )
    return doc.number


def _invoice_balance(db: Session, invoice_id: uuid.UUID) -> Decimal:
    invoice = db.get(SalesDocument, invoice_id)
    paid = receivables.paid_amounts(db, [invoice_id]).get(invoice_id, ZERO)
    return (invoice.total if invoice else ZERO) - paid


def _check_against_invoice(db: Session, note: SalesDocument) -> None:
    invoice = db.get(SalesDocument, note.invoice_id)
    if invoice is None or invoice.kind != "invoice" or invoice.status != "issued":
        raise Conflict("A credit note can only be raised against an issued invoice.")
    credited = db.scalar(
        select(func.coalesce(func.sum(SalesDocument.total), 0)).where(
            SalesDocument.invoice_id == invoice.id,
            SalesDocument.kind == "credit_note",
            SalesDocument.status == "issued",
        )
    )
    if (credited or ZERO) + note.total > invoice.total:
        raise Conflict(
            f"Credit notes against {invoice.number} would come to more than the invoice."
        )


def set_status(db: Session, actor: Actor, document_id: uuid.UUID, action: str) -> None:
    """A sent quote is accepted or declined; a confirmed order can be cancelled."""
    _require(db, actor, CREATE)
    doc = _locked(db, document_id)
    moves = {
        ("quote", "accept"): (("sent",), "accepted"),
        ("quote", "decline"): (("sent", "accepted"), "declined"),
        ("order", "cancel"): (("confirmed",), "cancelled"),
    }
    move = moves.get((doc.kind, action))
    if move is None or doc.status not in move[0]:
        raise Conflict("That can't be done to this document now.")
    before, doc.status = doc.status, move[1]
    db.flush()
    record_by(
        db,
        actor,
        f"finance.{doc.kind}.{action}",
        entity_type="sales_document",
        entity_id=str(doc.id),
        changes={"status": {"from": before, "to": doc.status}},
    )


def _copy(
    db: Session, actor: Actor, source: SalesDocument, kind: str, today: date
) -> SalesDocument:
    customer = db.get(Customer, source.customer_id)
    assert customer is not None  # noqa: S101  (foreign key)
    new = SalesDocument(
        kind=kind,
        status="draft",
        customer_id=source.customer_id,
        doc_date=today,
        due_date=today + timedelta(days=customer.payment_terms_days) if kind == "invoice" else None,
        reference=source.reference,
        memo=source.memo,
        stamp_duty_type_id=source.stamp_duty_type_id if kind != "quote" else None,
        created_by=actor.user_id,
    )
    db.add(new)
    db.flush()
    for line in _lines(db, source.id):
        db.add(
            SalesLine(
                document_id=new.id,
                position=line.position,
                description=line.description,
                account_id=line.account_id,
                cost_centre_id=line.cost_centre_id,
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
        _price(db, new)
    except Conflict:
        db.flush()
    return new


def convert(db: Session, actor: Actor, document_id: uuid.UUID, kind: str) -> uuid.UUID:
    """A draft order or invoice from a quote, or a draft invoice from an order."""
    _require(db, actor, CREATE)
    source = _locked(db, document_id)
    if kind not in CONVERSIONS.get(source.kind, ()):
        raise Conflict("That can't be converted to this.")
    if source.status not in CONVERTIBLE_FROM[source.kind]:
        raise Conflict("Only a sent or accepted quote, or a confirmed order, can be converted.")
    new = _copy(db, actor, source, kind, clock.today())
    new.source_document_id = source.id
    before, source.status = source.status, CONVERTED_STATUS[source.kind]
    db.flush()
    record_by(
        db,
        actor,
        f"finance.{source.kind}.convert",
        entity_type="sales_document",
        entity_id=str(source.id),
        changes={"status": {"from": before, "to": source.status}, "into": kind},
    )
    return new.id


def credit_note_for(db: Session, actor: Actor, invoice_id: uuid.UUID) -> uuid.UUID:
    """A draft credit note against an issued invoice, with its lines to trim."""
    _require(db, actor, CREATE)
    invoice = _locked(db, invoice_id)
    if invoice.kind != "invoice" or invoice.status != "issued":
        raise Conflict("A credit note can only be raised against an issued invoice.")
    note = _copy(db, actor, invoice, "credit_note", clock.today())
    note.invoice_id = invoice.id
    note.memo = f"Credit against {invoice.number}"
    db.flush()
    return note.id


def _naira(amount: Decimal) -> str:
    return f"NGN {amount:,.2f}"


def organisation_name(db: Session) -> str:
    found = org.get_organization(db)
    return (found.trading_name or found.legal_name) if found else "Kxmeleon"


def email_document(db: Session, actor: Actor, document_id: uuid.UUID) -> str:
    """Email an issued document's details to the customer. Returns the address."""
    _require(db, actor, CREATE)
    doc = db.get(SalesDocument, document_id)
    if doc is None:
        raise NotFound()
    if doc.status == "draft":
        raise Conflict("Issue it before emailing it.")
    customer = db.get(Customer, doc.customer_id)
    if customer is None or not customer.email:
        raise Conflict("Add the customer's email address first.")
    kind = KIND_LABELS[doc.kind]
    mail.queue(
        db,
        to=customer.email,
        template="finance_sales_document",
        context={
            "organisation": organisation_name(db),
            "customer": customer.name,
            "kind": kind.lower(),
            "kind_title": kind,
            "kind_article": "an" if kind[0] in "AEIOU" else "a",
            "number": doc.number,
            "date": f"{doc.doc_date:%d %b %Y}",
            "due": f"{doc.due_date:%d %b %Y}" if doc.due_date else "",
            "lines": [
                {"description": line.description, "amount": _naira(line.net + line.vat)}
                for line in _lines(db, doc.id)
            ],
            "net": _naira(doc.net),
            "vat": _naira(doc.vat),
            "stamp_duty": _naira(doc.stamp_duty) if doc.stamp_duty else "",
            "total": _naira(doc.total),
            "reference": doc.reference or "",
        },
    )
    record_by(
        db,
        actor,
        f"finance.{doc.kind}.email",
        entity_type="sales_document",
        entity_id=str(doc.id),
        changes={"to": customer.email},
    )
    return customer.email


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class LineView:
    description: str
    account_id: uuid.UUID
    account: str
    cost_centre_id: uuid.UUID | None
    cost_centre: str | None
    quantity: Decimal
    unit_price: Decimal
    net: Decimal
    vat_code: str
    vat_rate: Decimal
    vat: Decimal

    @property
    def vat_label(self) -> str:
        if self.vat_code == "standard" and self.vat_rate:
            return f"{self.vat_rate.normalize():f}%"
        return VAT_LABELS[self.vat_code]


@dataclass(frozen=True, slots=True)
class Settlement:
    kind: str  # receipt or credit_note
    id: uuid.UUID
    number: str
    day: date
    amount: Decimal
    wht: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: uuid.UUID
    kind: str
    number: str | None
    status: str  # stored status
    customer_id: uuid.UUID
    customer: str
    customer_email: str | None
    doc_date: date
    due_date: date | None
    valid_until: date | None
    reference: str | None
    memo: str | None
    net: Decimal
    vat: Decimal
    stamp_duty: Decimal
    stamp_duty_type_id: uuid.UUID | None
    stamp_duty_name: str | None
    total: Decimal
    paid: Decimal
    created_by: str | None
    created_at: datetime
    issued_by: str | None
    issued_at: datetime | None
    journal: tuple[uuid.UUID, str] | None
    source: tuple[uuid.UUID, str, str] | None  # id, kind, number
    invoice: tuple[uuid.UUID, str] | None  # a credit note's invoice
    derived: list[tuple[uuid.UUID, str, str | None, str]] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    lines: list[LineView] = field(default_factory=list)
    today: date | None = None
    can_edit: bool = False
    can_issue: bool = False
    issue_blocked: str | None = None
    can_act: bool = False  # convert, accept, email, raise a credit note

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def shown_status(self) -> str:
        return display_status(
            self.kind,
            self.status,
            total=self.total,
            paid=self.paid,
            due=self.due_date,
            today=self.today,
        )

    @property
    def days_overdue(self) -> int:
        if self.due_date and self.today and self.balance > 0:
            return max((self.today - self.due_date).days, 0)
        return 0

    @property
    def conversions(self) -> tuple[str, ...]:
        if self.status in CONVERTIBLE_FROM.get(self.kind, ()):
            return CONVERSIONS[self.kind]
        return ()


def display_status(
    kind: str,
    status: str,
    *,
    total: Decimal,
    paid: Decimal,
    due: date | None,
    today: date | None,
) -> str:
    if kind != "invoice" or status != "issued":
        return status
    if paid >= total:
        return "paid"
    if due and today and due < today:
        return "overdue"
    return "part_paid" if paid > 0 else "issued"


def _settlements(db: Session, doc: SalesDocument) -> list[Settlement]:
    rows = db.execute(
        select(Allocation, Receipt, SalesDocument)
        .outerjoin(Receipt, Receipt.id == Allocation.receipt_id)
        .outerjoin(SalesDocument, SalesDocument.id == Allocation.credit_note_id)
        .where(Allocation.invoice_id == doc.id)
        .order_by(Allocation.allocated_on, Allocation.created_at)
    ).all()
    out = []
    for alloc, receipt, note in rows:
        if alloc.receipt_id is not None:
            out.append(
                Settlement(
                    "receipt",
                    receipt.id,
                    receipt.number,
                    alloc.allocated_on,
                    alloc.amount,
                    receipt.wht,
                )
            )
        else:
            out.append(
                Settlement(
                    "credit_note", note.id, note.number or "", alloc.allocated_on, alloc.amount
                )
            )
    return out


def get_document(db: Session, actor: Actor, document_id: uuid.UUID, *, today: date) -> DocumentView:
    _require(db, actor, READ)
    doc = db.get(SalesDocument, document_id)
    if doc is None:
        raise NotFound()
    customer = db.get(Customer, doc.customer_id)
    assert customer is not None  # noqa: S101  (foreign key)
    names = journals.user_names(db, {doc.created_by, doc.issued_by})
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    centres = dict(org.cost_centre_choices(db))
    lines = [
        LineView(
            line.description,
            line.account_id,
            codes.get(line.account_id, ""),
            line.cost_centre_id,
            centres.get(line.cost_centre_id) if line.cost_centre_id else None,
            line.quantity,
            line.unit_price,
            line.net,
            line.vat_code,
            line.vat_rate,
            line.vat,
        )
        for line in _lines(db, doc.id)
    ]
    journal = db.get(Journal, doc.journal_id) if doc.journal_id else None
    source = db.get(SalesDocument, doc.source_document_id) if doc.source_document_id else None
    invoice = db.get(SalesDocument, doc.invoice_id) if doc.invoice_id else None
    derived = [
        (d.id, d.kind, d.number, d.status)
        for d in db.scalars(
            select(SalesDocument)
            .where(
                (SalesDocument.source_document_id == doc.id) | (SalesDocument.invoice_id == doc.id)
            )
            .order_by(SalesDocument.created_at)
        )
    ]
    paid = (
        receivables.paid_amounts(db, [doc.id]).get(doc.id, ZERO) if doc.kind == "invoice" else ZERO
    )
    if doc.kind == "credit_note" and doc.status == "issued":
        paid = (
            db.scalar(
                select(func.coalesce(func.sum(Allocation.amount), 0)).where(
                    Allocation.credit_note_id == doc.id
                )
            )
            or ZERO
        )
    draft = doc.status == "draft"
    can_create = _can(db, actor, CREATE)
    can_issue = draft and (
        _can(db, actor, ISSUE_CREDIT) if doc.kind == "credit_note" else can_create
    )
    blocked = None
    if can_issue and doc.kind == "credit_note" and _sod_blocked(db, actor, doc):
        can_issue, blocked = False, "You drafted this credit note, so someone else issues it."
    elif draft and doc.kind in ("invoice", "credit_note"):
        _, blocked = periods.period_for(db, doc.doc_date)
    duty = _stamp(db, doc.stamp_duty_type_id)
    return DocumentView(
        id=doc.id,
        kind=doc.kind,
        number=doc.number,
        status=doc.status,
        customer_id=customer.id,
        customer=customer.name,
        customer_email=customer.email,
        doc_date=doc.doc_date,
        due_date=doc.due_date,
        valid_until=doc.valid_until,
        reference=doc.reference,
        memo=doc.memo,
        net=doc.net,
        vat=doc.vat,
        stamp_duty=doc.stamp_duty,
        stamp_duty_type_id=doc.stamp_duty_type_id,
        stamp_duty_name=duty.name if duty else None,
        total=doc.total,
        paid=paid,
        created_by=names.get(doc.created_by) if doc.created_by else None,
        created_at=doc.created_at,
        issued_by=names.get(doc.issued_by) if doc.issued_by else None,
        issued_at=doc.issued_at,
        journal=(journal.id, journal.number) if journal and journal.number else None,
        source=(source.id, source.kind, source.number or "Draft") if source else None,
        invoice=(invoice.id, invoice.number or "") if invoice else None,
        derived=derived,
        settlements=_settlements(db, doc) if doc.kind == "invoice" else [],
        lines=lines,
        today=today,
        can_edit=draft and can_create,
        can_issue=can_issue and not blocked,
        issue_blocked=blocked,
        can_act=can_create and not draft,
    )


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: uuid.UUID
    kind: str
    number: str | None
    customer_id: uuid.UUID
    customer: str
    doc_date: date
    due_date: date | None
    valid_until: date | None
    net: Decimal
    vat: Decimal
    total: Decimal
    paid: Decimal
    status: str  # shown status
    source_number: str | None

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


FILTERS = {
    "invoice": ("draft", "issued", "part_paid", "paid", "overdue"),
    "quote": ("draft", "sent", "accepted", "declined", "converted"),
    "order": ("draft", "confirmed", "invoiced", "cancelled"),
    "credit_note": ("draft", "issued"),
}


def list_documents(
    db: Session,
    actor: Actor,
    kind: str,
    *,
    customer_id: uuid.UUID | None,
    status: str,
    start: date | None,
    end: date | None,
    today: date,
) -> list[DocumentRow]:
    _require(db, actor, READ)
    query = (
        select(SalesDocument, Customer.name)
        .join(Customer, Customer.id == SalesDocument.customer_id)
        .where(SalesDocument.kind == kind)
    )
    if customer_id:
        query = query.where(SalesDocument.customer_id == customer_id)
    if start:
        query = query.where(SalesDocument.doc_date >= start)
    if end:
        query = query.where(SalesDocument.doc_date <= end)
    rows = db.execute(
        query.order_by(SalesDocument.doc_date.desc(), SalesDocument.number.desc()).limit(500)
    ).all()
    docs = [doc for doc, _ in rows]
    paid = receivables.paid_amounts(db, [d.id for d in docs]) if kind == "invoice" else {}
    sources = {
        d.id: d.number
        for d in db.scalars(
            select(SalesDocument).where(
                SalesDocument.id.in_([d.source_document_id for d in docs if d.source_document_id])
            )
        )
    }
    out = []
    for doc, name in rows:
        settled = paid.get(doc.id, ZERO)
        shown = display_status(
            kind, doc.status, total=doc.total, paid=settled, due=doc.due_date, today=today
        )
        if status and shown != status:
            continue
        out.append(
            DocumentRow(
                doc.id,
                doc.kind,
                doc.number,
                doc.customer_id,
                name,
                doc.doc_date,
                doc.due_date,
                doc.valid_until,
                doc.net,
                doc.vat,
                doc.total,
                settled,
                shown,
                sources.get(doc.source_document_id) if doc.source_document_id else None,
            )
        )
    return out


def counts(db: Session) -> dict[str, int]:
    found = dict(
        db.execute(select(SalesDocument.kind, func.count()).group_by(SalesDocument.kind)).all()
    )
    found["receipt"] = db.scalar(select(func.count()).select_from(Receipt)) or 0
    return {kind: found.get(kind, 0) for kind in (*DOCUMENT_KINDS, "receipt")}


def issued_invoices_for(db: Session, customer_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """Issued invoices of the customer, newest first, for a credit note's picker."""
    return [
        (d.id, d.number or "")
        for d in db.scalars(
            select(SalesDocument)
            .where(
                SalesDocument.customer_id == customer_id,
                SalesDocument.kind == "invoice",
                SalesDocument.status == "issued",
            )
            .order_by(SalesDocument.doc_date.desc())
        )
    ]


def seller(db: Session) -> pdf.Party:
    found = org.get_organization(db)
    if found is None:
        return pdf.Party("Kxmeleon", None, None)
    return pdf.Party(
        found.trading_name or found.legal_name,
        found.address,
        found.tin,
        f"RC {found.rc_number}" if found.rc_number else None,
    )


def buyer(customer: Customer) -> pdf.Party:
    return pdf.Party(customer.name, customer.address, customer.tin, customer.code)


def document_pdf(db: Session, actor: Actor, document_id: uuid.UUID) -> tuple[str, bytes]:
    """(filename, PDF) of an issued document."""
    view = get_document(db, actor, document_id, today=clock.today())
    if view.number is None:
        raise Conflict("A draft has no PDF; issue it first.")
    customer = db.get(Customer, view.customer_id)
    assert customer is not None  # noqa: S101  (foreign key)
    facts = [("Date", pdf.day(view.doc_date))]
    if view.due_date:
        facts.append(("Due", pdf.day(view.due_date)))
    if view.valid_until:
        facts.append(("Valid until", pdf.day(view.valid_until)))
    if view.reference:
        facts.append(("Your reference", view.reference))
    if view.invoice:
        facts.append(("Against invoice", view.invoice[1]))
    totals = [("Net", pdf.money(view.net)), ("VAT", pdf.money(view.vat))]
    if view.stamp_duty:
        totals.append((f"Stamp duty ({view.stamp_duty_name})", pdf.money(view.stamp_duty)))
    totals.append(("Total", pdf.money(view.total)))
    if view.kind == "invoice" and view.paid:
        totals += [("Paid", pdf.money(view.paid)), ("Balance due", pdf.money(view.balance))]
    lines = [
        (
            line.description,
            f"{line.quantity.normalize():f}",
            f"{line.unit_price:,.2f}",
            line.vat_label,
            f"{line.net:,.2f}",
        )
        for line in view.lines
    ]
    data = pdf.DocumentPdf(
        view.kind_label, view.number, seller(db), buyer(customer), facts, lines, totals, view.memo
    )
    return f"{view.number}.pdf", pdf.render_document(data)
