"""Receivables web routes (ADR 0024): customers, quotes, sales orders, invoices, credit
notes, receipts and aging. Routes translate forms and render; the services do the work."""

import csv
import io
import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import customers, periods, receipts, receivables, sales, tax
from app.modules.finance.customers import CustomerInput
from app.modules.finance.models import VAT_CODES, Customer, SalesDocument
from app.modules.finance.sales import DocumentInput, LineInput
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

EMPTY_ROWS = 2
MIN_ROWS = 3
TABS = ("invoice", "quote", "order", "credit_note", "receipt")
TAB_LABELS = {
    "invoice": "Invoices",
    "quote": "Quotes",
    "order": "Orders",
    "credit_note": "Credit notes",
    "receipt": "Receipts",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="finance", active_section=section, crumbs=["Finance", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _download(filename: str, body: bytes, media_type: str) -> Response:
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ customers


def _customers_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    q, show = extra.pop("q", ""), extra.pop("show", "active")
    return {
        "shell": _shell(db, actor, "Customers"),
        "rows": customers.list_customers(db, actor, q=q, show=show, today=clock.today()),
        "filters": {"q": q, "show": show},
        "can_manage": _can(db, actor, customers.MANAGE),
        "default_terms": customers.default_terms(db),
        "editing": "",
        "customer": None,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Customers",
        **extra,
    }


@router.get("/finance/customers", response_class=HTMLResponse)
@requires("finance.customer.read")
@screen("finance.customers")
def customer_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    show: str = "active",
    edit: str = "",
    notice: str = "",
) -> HTMLResponse:
    show = show if show in customers.SHOWS else "active"
    editing = db.get(Customer, _uuid(edit)) if _uuid(edit) else None
    context = _customers_context(
        db,
        actor,
        q=q,
        show=show,
        editing="new" if edit == "new" else (str(editing.id) if editing else ""),
        customer=editing,
        notice="Saved." if notice else None,
    )
    return _render(request, "finance/customers.html", context)


@router.post("/finance/customers", response_class=HTMLResponse)
@requires("finance.customer.manage")
@screen("finance.customers")
def save_customer(
    request: Request,
    db: Db,
    actor: CurrentActor,
    customer_id: FormStr = "",
    name: FormStr = "",
    email: FormStr = "",
    phone: FormStr = "",
    address: FormStr = "",
    tin: FormStr = "",
    payment_terms_days: FormStr = "",
    reminders: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = CustomerInput(
        name,
        email,
        phone,
        address,
        tin,
        payment_terms_days,
        bool(reminders),
        bool(is_active) or not customer_id,
    )
    try:
        with db.begin_nested():
            saved = customers.save(db, actor, _uuid(customer_id), data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "name": name,
            "email": email,
            "phone": phone,
            "address": address,
            "tin": tin,
            "payment_terms_days": payment_terms_days,
            "reminders": reminders,
            "is_active": is_active,
        }
        context = _customers_context(
            db,
            actor,
            editing=customer_id or "new",
            customer=db.get(Customer, _uuid(customer_id)) if _uuid(customer_id) else None,
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/customers.html", context, 422)
    return _see_other(f"/finance/customers/{saved}?notice=saved")


def _customer_context(
    db: Session, actor: Actor, customer_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    customer = customers.get(db, actor, customer_id)
    today = clock.today()
    start = extra.pop("start", None) or today.replace(day=1) - timedelta(days=60)
    end = extra.pop("end", None) or today
    return {
        "shell": _shell(db, actor, "Customers"),
        "customer": customer,
        "summary": receivables.customer_summary(db, customer.id, today=today),
        "buckets": receivables.BUCKETS,
        "open_invoices": receivables.open_invoices(db, customer.id, as_at=today),
        "credits": receivables.credits(db, customer.id),
        "statement": receivables.statement(db, customer, start=start, end=end),
        "start": start,
        "end": end,
        "can_manage": _can(db, actor, customers.MANAGE),
        "can_sell": _can(db, actor, sales.CREATE),
        "can_receive": _can(db, actor, receipts.CREATE),
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": customer.name,
        **extra,
    }


NOTICES = {
    "saved": "Saved.",
    "reminded": "Reminder queued to send.",
    "allocated": "Allocated.",
}


@router.get("/finance/customers/{customer_id:uuid}", response_class=HTMLResponse)
@requires("finance.customer.read")
@screen("finance.customer")
def customer_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    customer_id: uuid.UUID,
    start: str = "",
    end: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _customer_context(
        db,
        actor,
        customer_id,
        start=parse_day(start),
        end=parse_day(end),
        notice=NOTICES.get(notice),
    )
    return _render(request, "finance/customer.html", context)


@router.post("/finance/customers/{customer_id:uuid}/remind", response_class=HTMLResponse)
@requires("finance.customer.manage")
@screen("finance.customer")
def remind_customer(
    request: Request, db: Db, actor: CurrentActor, customer_id: uuid.UUID
) -> Response:
    try:
        with db.begin_nested():
            customers.remind_now(db, actor, customer_id, today=clock.today())
    except (Conflict, PermissionDenied) as exc:
        context = _customer_context(db, actor, customer_id, errors={"form": exc.message})
        return _render(request, "finance/customer.html", context, 422)
    return _see_other(f"/finance/customers/{customer_id}?notice=reminded")


@router.post("/finance/customers/{customer_id:uuid}/allocate", response_class=HTMLResponse)
@requires("finance.receipt.create")
@screen("finance.customer")
def allocate_credit(
    request: Request,
    db: Db,
    actor: CurrentActor,
    customer_id: uuid.UUID,
    credit: FormStr = "",
    invoice_id: FormStr = "",
    amount: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            receipts.allocate(
                db,
                actor,
                customer_id,
                credit=credit,
                invoice_id=invoice_id,
                amount=amount,
                today=clock.today(),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _customer_context(
            db,
            actor,
            customer_id,
            values={"credit": credit, "invoice_id": invoice_id, "amount": amount},
            errors=_errors(exc),
        )
        return _render(request, "finance/customer.html", context, 422)
    return _see_other(f"/finance/customers/{customer_id}?notice=allocated")


@router.get("/finance/customers/{customer_id:uuid}/statement.pdf")
@requires("finance.customer.read")
def statement_pdf(
    db: Db, actor: CurrentActor, customer_id: uuid.UUID, start: str = "", end: str = ""
) -> Response:
    today = clock.today()
    first = parse_day(start) or today.replace(day=1) - timedelta(days=60)
    last = parse_day(end) or today
    filename, body = customers.statement_pdf(db, actor, customer_id, start=first, end=last)
    return _download(filename, body, "application/pdf")


# ------------------------------------------------------------------ sales documents


@router.get("/finance/sales", response_class=HTMLResponse)
@requires("finance.sales.read")
@screen("finance.sales")
def sales_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "invoice",
    customer: str = "",
    status: str = "",
    start: str = "",
    end: str = "",
    notice: str = "",
) -> HTMLResponse:
    tab = tab if tab in TABS else "invoice"
    today = clock.today()
    first, last, who = parse_day(start), parse_day(end), _uuid(customer)
    if tab == "receipt":
        rows: list[Any] = receipts.list_receipts(
            db, actor, customer_id=who, start=first, end=last, awaiting=status == "awaiting"
        )
    else:
        rows = sales.list_documents(
            db, actor, tab, customer_id=who, status=status, start=first, end=last, today=today
        )
    context = {
        "shell": _shell(db, actor, "Sales"),
        "tab": tab,
        "tabs": [(k, TAB_LABELS[k]) for k in TABS],
        "rows": rows,
        "counts": sales.counts(db),
        "statuses": sales.FILTERS.get(tab, ("awaiting",)),
        "status_labels": {**sales.STATUS_LABELS, "awaiting": "Awaiting WHT credit note"},
        "tones": sales.STATUS_TONES,
        "customers": customers.choices(db),
        "filters": {"customer": customer, "status": status, "start": start, "end": end},
        "can_create": _can(db, actor, sales.CREATE),
        "can_receive": _can(db, actor, receipts.CREATE),
        "notice": {"deleted": "Draft deleted."}.get(notice),
        "page_title": "Sales",
    }
    return _render(request, "finance/sales.html", context)


def _line_inputs(
    description: list[str],
    account_id: list[str],
    cost_centre_id: list[str],
    quantity: list[str],
    unit_price: list[str],
    vat_code: list[str],
) -> list[LineInput]:
    rows = max(len(description), len(account_id), len(unit_price))

    def at(values: list[str], i: int) -> str:
        return values[i] if i < len(values) else ""

    return [
        LineInput(
            at(description, i),
            at(account_id, i),
            at(cost_centre_id, i),
            at(quantity, i),
            at(unit_price, i),
            at(vat_code, i) or "standard",
        )
        for i in range(rows)
    ]


def _form_rows(lines: list[LineInput]) -> list[LineInput]:
    blank = LineInput("", "", "", "", "", "standard")
    count = max(len(lines) + EMPTY_ROWS, MIN_ROWS)
    return [*lines, *([blank] * (count - len(lines)))]


def _document_context(
    db: Session, actor: Actor, document_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    today = clock.today()
    view = sales.get_document(db, actor, document_id, today=today) if document_id else None
    kind = view.kind if view else extra.pop("kind", "invoice")
    lines = extra.pop("lines", None)
    if lines is None:
        lines = [
            LineInput(
                line.description,
                str(line.account_id),
                str(line.cost_centre_id or ""),
                f"{line.quantity.normalize():f}",
                f"{line.unit_price:.2f}",
                line.vat_code,
            )
            for line in (view.lines if view else [])
        ]
    customer_id = view.customer_id if view else _uuid(extra.get("values", {}).get("customer_id"))
    return {
        "shell": _shell(db, actor, "Sales"),
        "doc": view,
        "kind": kind,
        "kind_label": sales.KIND_LABELS[kind],
        "rows": _form_rows(lines),
        "customers": customers.choices(db),
        "income_accounts": sales.income_accounts(db),
        "cost_centres": org.cost_centre_choices(db),
        "vat_codes": [(c, sales.VAT_LABELS[c]) for c in VAT_CODES],
        "vat_rate": tax.vat_rate_on(db, today),
        "stamp_types": tax.stamp_types(db, active_only=True),
        "invoices": sales.issued_invoices_for(db, customer_id) if customer_id else [],
        "status_labels": sales.STATUS_LABELS,
        "tones": sales.STATUS_TONES,
        "kind_labels": sales.KIND_LABELS,
        "can_issue_credit": _can(db, actor, sales.ISSUE_CREDIT),
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (view.number if view and view.number else f"New {kind.replace('_', ' ')}"),
        **extra,
    }


DOC_NOTICES = {
    "saved": "Draft saved.",
    "issued": "Issued and posted.",
    "sent": "Marked as sent.",
    "confirmed": "Order confirmed.",
    "status": "Updated.",
    "emailed": "Email queued to send.",
    "converted": "Converted. This is the new draft.",
    "credit": "Credit note drafted. Someone else issues it.",
}


@router.get("/finance/sales/new", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def new_document(
    request: Request, db: Db, actor: CurrentActor, kind: str = "invoice", customer: str = ""
) -> HTMLResponse:
    kind = kind if kind in sales.KIND_LABELS else "invoice"
    context = _document_context(db, actor, None, kind=kind, values={"customer_id": customer})
    return _render(request, "finance/sales_document.html", context)


@router.get("/finance/sales/{document_id:uuid}", response_class=HTMLResponse)
@requires("finance.sales.read")
@screen("finance.sales_document")
def document_page(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _document_context(db, actor, document_id, notice=DOC_NOTICES.get(notice))
    return _render(request, "finance/sales_document.html", context)


@router.post("/finance/sales", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def save_document(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: FormStr = "",
    kind: FormStr = "invoice",
    customer_id: FormStr = "",
    doc_date: FormStr = "",
    due_date: FormStr = "",
    valid_until: FormStr = "",
    reference: FormStr = "",
    memo: FormStr = "",
    stamp_duty_type_id: FormStr = "",
    description: FormList = [],  # noqa: B006  (FastAPI form lists)
    account_id: FormList = [],  # noqa: B006
    cost_centre_id: FormList = [],  # noqa: B006
    quantity: FormList = [],  # noqa: B006
    unit_price: FormList = [],  # noqa: B006
    vat_code: FormList = [],  # noqa: B006
) -> Response:
    lines = _line_inputs(description, account_id, cost_centre_id, quantity, unit_price, vat_code)
    data = DocumentInput(
        kind,
        customer_id,
        parse_day(doc_date),
        parse_day(due_date),
        parse_day(valid_until),
        reference,
        memo,
        stamp_duty_type_id,
        lines,
    )
    existing = _uuid(document_id)
    try:
        with db.begin_nested():
            saved = sales.save_draft(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "customer_id": customer_id,
            "doc_date": doc_date,
            "due_date": due_date,
            "valid_until": valid_until,
            "reference": reference,
            "memo": memo,
            "stamp_duty_type_id": stamp_duty_type_id,
        }
        context = _document_context(
            db,
            actor,
            existing,
            kind=kind if kind in sales.KIND_LABELS else "invoice",
            lines=[line for line in lines if line.description or line.unit_price],
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/sales_document.html", context, 422)
    return _see_other(f"/finance/sales/{saved}?notice=saved")


def _document_action(
    request: Request,
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    run: Any,
    notice: str,
) -> Response:
    try:
        with db.begin_nested():
            target = run()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _document_context(db, actor, document_id, errors=_errors(exc))
        return _render(request, "finance/sales_document.html", context, 422)
    target_id = target if isinstance(target, uuid.UUID) else document_id
    return _see_other(f"/finance/sales/{target_id}?notice={notice}")


ISSUE_NOTICES = {"quote": "sent", "order": "confirmed"}


@router.post("/finance/sales/{document_id:uuid}/issue", response_class=HTMLResponse)
@requires("finance.sales.read")
@screen("finance.sales_document")
def issue_document(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID
) -> Response:
    doc = db.get(SalesDocument, document_id)
    if doc is None:
        raise NotFound()
    notice = ISSUE_NOTICES.get(doc.kind, "issued")
    return _document_action(
        request, db, actor, document_id, lambda: sales.issue(db, actor, document_id), notice
    )


@router.post("/finance/sales/{document_id:uuid}/delete", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def delete_document(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID
) -> Response:
    try:
        with db.begin_nested():
            kind = sales.delete_draft(db, actor, document_id)
    except (Conflict, PermissionDenied) as exc:
        context = _document_context(db, actor, document_id, errors={"form": exc.message})
        return _render(request, "finance/sales_document.html", context, 422)
    return _see_other(f"/finance/sales?tab={kind}&notice=deleted")


@router.post("/finance/sales/{document_id:uuid}/status", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def document_status(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, action: FormStr = ""
) -> Response:
    return _document_action(
        request,
        db,
        actor,
        document_id,
        lambda: sales.set_status(db, actor, document_id, action),
        "status",
    )


@router.post("/finance/sales/{document_id:uuid}/convert", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def convert_document(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, to: FormStr = ""
) -> Response:
    return _document_action(
        request,
        db,
        actor,
        document_id,
        lambda: sales.convert(db, actor, document_id, to),
        "converted",
    )


@router.post("/finance/sales/{document_id:uuid}/credit-note", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def raise_credit_note(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID
) -> Response:
    return _document_action(
        request,
        db,
        actor,
        document_id,
        lambda: sales.credit_note_for(db, actor, document_id),
        "credit",
    )


@router.post("/finance/sales/{document_id:uuid}/email", response_class=HTMLResponse)
@requires("finance.sales.create")
@screen("finance.sales_document")
def email_document(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID
) -> Response:
    return _document_action(
        request,
        db,
        actor,
        document_id,
        lambda: sales.email_document(db, actor, document_id),
        "emailed",
    )


@router.get("/finance/sales/{document_id:uuid}/pdf")
@requires("finance.sales.read")
def document_pdf(db: Db, actor: CurrentActor, document_id: uuid.UUID) -> Response:
    filename, body = sales.document_pdf(db, actor, document_id)
    return _download(filename, body, "application/pdf")


# ------------------------------------------------------------------ receipts


def _receipt_context(
    db: Session, actor: Actor, receipt_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = receipts.get_receipt(db, actor, receipt_id) if receipt_id else None
    values = extra.get("values", {})
    customer_id = _uuid(values.get("customer_id"))
    today = clock.today()
    return {
        "shell": _shell(db, actor, "Sales"),
        "receipt": view,
        "customers": customers.choices(db),
        "open_invoices": receivables.open_invoices(db, customer_id, as_at=today)
        if customer_id
        else [],
        "banks": receipts.bank_accounts(db),
        "wht_types": tax.wht_types(db, active_only=True),
        "allocations": extra.pop("allocations", {}),
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": view.number if view else "Record receipt",
        **extra,
    }


@router.get("/finance/receipts/new", response_class=HTMLResponse)
@requires("finance.receipt.create")
@screen("finance.receipt")
def new_receipt(request: Request, db: Db, actor: CurrentActor, customer: str = "") -> HTMLResponse:
    context = _receipt_context(db, actor, None, values={"customer_id": customer})
    return _render(request, "finance/receipt.html", context)


@router.post("/finance/receipts", response_class=HTMLResponse)
@requires("finance.receipt.create")
@screen("finance.receipt")
def record_receipt(
    request: Request,
    db: Db,
    actor: CurrentActor,
    customer_id: FormStr = "",
    receipt_date: FormStr = "",
    bank_account_id: FormStr = "",
    reference: FormStr = "",
    cash: FormStr = "",
    wht: FormStr = "",
    wht_type_id: FormStr = "",
    invoice_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    allocate: FormList = [],  # noqa: B006
    step: FormStr = "",
) -> Response:
    values = {
        "customer_id": customer_id,
        "receipt_date": receipt_date,
        "bank_account_id": bank_account_id,
        "reference": reference,
        "cash": cash,
        "wht": wht,
        "wht_type_id": wht_type_id,
    }
    pairs = list(zip(invoice_id, allocate, strict=False))
    if step == "customer":  # the customer changed: show their open invoices
        context = _receipt_context(db, actor, None, values=values)
        return _render(request, "finance/receipt.html", context)
    data = receipts.ReceiptInput(
        customer_id,
        parse_day(receipt_date),
        bank_account_id,
        reference,
        cash,
        wht,
        wht_type_id,
        pairs,
    )
    try:
        with db.begin_nested():
            saved = receipts.record(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _receipt_context(
            db, actor, None, values=values, allocations=dict(pairs), errors=_errors(exc)
        )
        return _render(request, "finance/receipt.html", context, 422)
    return _see_other(f"/finance/receipts/{saved}?notice=recorded")


RECEIPT_NOTICES = {
    "recorded": "Receipt recorded and posted.",
    "reversed": "Receipt reversed. The invoices it settled are open again.",
    "certificate": "WHT credit note recorded.",
}


@router.get("/finance/receipts/{receipt_id:uuid}", response_class=HTMLResponse)
@requires("finance.sales.read")
@screen("finance.receipt")
def receipt_page(
    request: Request, db: Db, actor: CurrentActor, receipt_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _receipt_context(db, actor, receipt_id, notice=RECEIPT_NOTICES.get(notice))
    return _render(request, "finance/receipt.html", context)


@router.post("/finance/receipts/{receipt_id:uuid}/reverse", response_class=HTMLResponse)
@requires("finance.receipt.reverse")
@screen("finance.receipt")
def reverse_receipt(
    request: Request,
    db: Db,
    actor: CurrentActor,
    receipt_id: uuid.UUID,
    reverse_on: FormStr = "",
    reason: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            receipts.reverse(db, actor, receipt_id, on=parse_day(reverse_on), reason=reason)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _receipt_context(
            db,
            actor,
            receipt_id,
            values={"reverse_on": reverse_on, "reason": reason},
            errors=_errors(exc),
        )
        return _render(request, "finance/receipt.html", context, 422)
    return _see_other(f"/finance/receipts/{receipt_id}?notice=reversed")


@router.post("/finance/receipts/{receipt_id:uuid}/certificate", response_class=HTMLResponse)
@requires("finance.receipt.create")
@screen("finance.receipt")
def receipt_certificate(
    request: Request,
    db: Db,
    actor: CurrentActor,
    receipt_id: uuid.UUID,
    certificate_number: FormStr = "",
    certificate_date: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            receipts.record_certificate(
                db,
                actor,
                receipt_id,
                number=certificate_number,
                received_on=parse_day(certificate_date),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _receipt_context(
            db,
            actor,
            receipt_id,
            values={"certificate_number": certificate_number, "certificate_date": certificate_date},
            errors=_errors(exc),
        )
        return _render(request, "finance/receipt.html", context, 422)
    return _see_other(f"/finance/receipts/{receipt_id}?notice=certificate")


# ------------------------------------------------------------------ aging


def _as_at(value: str) -> date:
    return parse_day(value) or clock.today()


@router.get("/finance/aging", response_class=HTMLResponse)
@requires("finance.customer.read")
@screen("finance.aging")
def aging_page(request: Request, db: Db, actor: CurrentActor, as_at: str = "") -> HTMLResponse:
    at = _as_at(as_at)
    settings = periods.settings(db)
    context = {
        "shell": _shell(db, actor, "Aging"),
        "report": receivables.aging(db, as_at=at),
        "buckets": receivables.BUCKETS,
        "settings": settings,
        "as_at": at,
        "can_settings": _can(db, actor, tax.READ),
        "page_title": "Aging",
    }
    return _render(request, "finance/aging.html", context)


@router.get("/finance/aging.csv")
@requires("finance.customer.read")
def aging_csv(db: Db, actor: CurrentActor, as_at: str = "") -> Response:
    at = _as_at(as_at)
    report = receivables.aging(db, as_at=at)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["Code", "Customer", *receivables.BUCKETS, "Unallocated credit", "Total"])
    for row in report.rows:
        name = "'" + row.name if row.name[:1] in {"=", "+", "-", "@"} else row.name
        writer.writerow(
            [
                row.code,
                name,
                *(f"{b:.2f}" for b in row.buckets),
                f"{row.credit:.2f}",
                f"{row.total:.2f}",
            ]
        )
    writer.writerow(
        [
            "",
            "Total",
            *(f"{b:.2f}" for b in report.totals),
            f"{report.credit:.2f}",
            f"{report.total:.2f}",
        ]
    )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="aging-{at.isoformat()}.csv"'},
    )
