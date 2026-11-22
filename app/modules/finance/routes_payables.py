"""Payables web routes (ADR 0025): suppliers, purchase orders and goods received, bills and
debit notes, supplier payments and aging. Routes translate forms and render; the services
do the work."""

import csv
import io
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import (
    bills,
    payables,
    payments,
    periods,
    purchasing,
    receipts,
    receivables,
    sales,
    suppliers,
    tax,
)
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.models import SUPPLIER_KINDS, VAT_CODES, Supplier, WhtType
from app.modules.finance.sales import LineInput
from app.modules.finance.suppliers import SupplierInput
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
TABS = ("bill", "order", "debit_note", "payment")
TAB_LABELS = {
    "bill": "Bills",
    "order": "Purchase orders",
    "debit_note": "Debit notes",
    "payment": "Payments",
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


def _at(values: list[str], i: int) -> str:
    return values[i] if i < len(values) else ""


# ------------------------------------------------------------------ suppliers


def _suppliers_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    q, show = extra.pop("q", ""), extra.pop("show", "active")
    return {
        "shell": _shell(db, actor, "Suppliers"),
        "rows": suppliers.list_suppliers(db, actor, q=q, show=show, today=clock.today()),
        "filters": {"q": q, "show": show},
        "kinds": [(k, suppliers.KIND_LABELS[k]) for k in SUPPLIER_KINDS],
        "kind_labels": suppliers.KIND_LABELS,
        "wht_types": tax.wht_types(db, active_only=True),
        "can_manage": _can(db, actor, suppliers.MANAGE),
        "default_terms": periods.settings(db).default_terms_days,
        "editing": "",
        "supplier": None,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Suppliers",
        **extra,
    }


@router.get("/finance/suppliers", response_class=HTMLResponse)
@requires("finance.supplier.read")
@screen("finance.suppliers")
def supplier_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    show: str = "active",
    edit: str = "",
) -> HTMLResponse:
    show = show if show in suppliers.SHOWS else "active"
    editing = db.get(Supplier, _uuid(edit)) if _uuid(edit) else None
    context = _suppliers_context(
        db,
        actor,
        q=q,
        show=show,
        editing="new" if edit == "new" else (str(editing.id) if editing else ""),
        supplier=editing,
    )
    return _render(request, "finance/suppliers.html", context)


@router.post("/finance/suppliers", response_class=HTMLResponse)
@requires("finance.supplier.manage")
@screen("finance.suppliers")
def save_supplier(
    request: Request,
    db: Db,
    actor: CurrentActor,
    supplier_id: FormStr = "",
    name: FormStr = "",
    kind: FormStr = "company",
    email: FormStr = "",
    phone: FormStr = "",
    address: FormStr = "",
    tin: FormStr = "",
    payment_terms_days: FormStr = "",
    wht_type_id: FormStr = "",
    bank_name: FormStr = "",
    bank_code: FormStr = "",
    account_number: FormStr = "",
    account_name: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = SupplierInput(
        name,
        kind,
        email,
        phone,
        address,
        tin,
        payment_terms_days,
        wht_type_id,
        bank_name,
        bank_code,
        account_number,
        account_name,
        bool(is_active) or not supplier_id,
    )
    try:
        with db.begin_nested():
            saved = suppliers.save(db, actor, _uuid(supplier_id), data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "name": name,
            "kind": kind,
            "email": email,
            "phone": phone,
            "address": address,
            "tin": tin,
            "payment_terms_days": payment_terms_days,
            "wht_type_id": wht_type_id,
            "bank_name": bank_name,
            "bank_code": bank_code,
            "account_number": account_number,
            "account_name": account_name,
            "is_active": is_active,
        }
        context = _suppliers_context(
            db,
            actor,
            editing=supplier_id or "new",
            supplier=db.get(Supplier, _uuid(supplier_id)) if _uuid(supplier_id) else None,
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/suppliers.html", context, 422)
    return _see_other(f"/finance/suppliers/{saved}?notice=saved")


@router.get("/finance/suppliers/{supplier_id:uuid}", response_class=HTMLResponse)
@requires("finance.supplier.read")
@screen("finance.supplier")
def supplier_page(
    request: Request, db: Db, actor: CurrentActor, supplier_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    supplier = suppliers.get(db, actor, supplier_id)
    today = clock.today()
    wht = db.get(WhtType, supplier.wht_type_id) if supplier.wht_type_id else None
    context = {
        "shell": _shell(db, actor, "Suppliers"),
        "supplier": supplier,
        "kind_label": suppliers.KIND_LABELS[supplier.kind],
        "wht_type": wht.name if wht else None,
        "summary": payables.supplier_summary(db, supplier.id, today=today),
        "buckets": receivables.BUCKETS,
        "open_bills": payables.open_bills(db, supplier.id, as_at=today),
        "orders": purchasing.open_orders_for(db, supplier.id),
        "order_labels": purchasing.STATUS_LABELS,
        "order_tones": purchasing.STATUS_TONES,
        "can_manage": _can(db, actor, suppliers.MANAGE),
        "can_buy": _can(db, actor, purchasing.CREATE),
        "can_pay": _can(db, actor, payments.CREATE),
        "notice": "Saved." if notice == "saved" else None,
        "page_title": supplier.name,
    }
    return _render(request, "finance/supplier.html", context)


# ------------------------------------------------------------------ the purchases list


@router.get("/finance/purchases", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.purchases")
def purchases_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "bill",
    supplier: str = "",
    status: str = "",
    start: str = "",
    end: str = "",
    notice: str = "",
) -> HTMLResponse:
    tab = tab if tab in TABS else "bill"
    first, last, who = parse_day(start), parse_day(end), _uuid(supplier)
    rows: list[Any]
    if tab == "order":
        rows = purchasing.list_orders(
            db, actor, supplier_id=who, status=status, start=first, end=last
        )
        statuses = tuple(purchasing.STATUS_LABELS)
        labels, tones = purchasing.STATUS_LABELS, purchasing.STATUS_TONES
    elif tab == "payment":
        rows = payments.list_payments(
            db, actor, supplier_id=who, status=status, start=first, end=last
        )
        statuses = tuple(payments.STATUS_LABELS)
        labels, tones = payments.STATUS_LABELS, payments.STATUS_TONES
    else:
        rows = bills.list_bills(
            db,
            actor,
            tab,
            supplier_id=who,
            status=status,
            start=first,
            end=last,
            today=clock.today(),
        )
        statuses = bills.BILL_FILTERS[tab]
        labels, tones = bills.STATUS_LABELS, bills.STATUS_TONES
    context = {
        "shell": _shell(db, actor, "Purchases"),
        "tab": tab,
        "tabs": [(k, TAB_LABELS[k]) for k in TABS],
        "rows": rows,
        "statuses": statuses,
        "status_labels": labels,
        "tones": tones,
        "suppliers": suppliers.choices(db),
        "filters": {"supplier": supplier, "status": status, "start": start, "end": end},
        "can_create": _can(db, actor, purchasing.CREATE),
        "can_pay": _can(db, actor, payments.CREATE),
        "notice": {"deleted": "Deleted."}.get(notice),
        "page_title": "Purchases",
    }
    return _render(request, "finance/purchases.html", context)


# ------------------------------------------------------------------ purchase orders


def _line_inputs(
    description: list[str],
    account_id: list[str],
    cost_centre_id: list[str],
    quantity: list[str],
    unit_price: list[str],
    vat_code: list[str],
) -> list[LineInput]:
    rows = max(len(description), len(account_id), len(unit_price))
    return [
        LineInput(
            _at(description, i),
            _at(account_id, i),
            _at(cost_centre_id, i),
            _at(quantity, i),
            _at(unit_price, i),
            _at(vat_code, i) or "standard",
        )
        for i in range(rows)
    ]


def _form_rows(lines: list[Any], blank: Any) -> list[Any]:
    count = max(len(lines) + EMPTY_ROWS, MIN_ROWS)
    return [*lines, *([blank] * (count - len(lines)))]


def _pickers(db: Session) -> dict[str, Any]:
    return {
        "cost_accounts": purchasing.cost_accounts(db),
        "cost_centres": org.cost_centre_choices(db),
        "vat_codes": [(c, sales.VAT_LABELS[c]) for c in VAT_CODES],
        "vat_rate": tax.vat_rate_on(db, clock.today()),
    }


def _order_context(
    db: Session, actor: Actor, order_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = purchasing.get_order(db, actor, order_id) if order_id else None
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
    return {
        "shell": _shell(db, actor, "Purchases"),
        "order": view,
        "rows": _form_rows(lines, LineInput("", "", "", "", "", "standard")),
        "suppliers": suppliers.choices(db),
        "status_labels": purchasing.STATUS_LABELS,
        "tones": purchasing.STATUS_TONES,
        "bill_labels": bills.STATUS_LABELS,
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (view.number or "Draft purchase order") if view else "New purchase order",
        **_pickers(db),
        **extra,
    }


ORDER_NOTICES = {
    "saved": "Draft saved. Someone other than you approves it.",
    "approved": "Approved.",
    "received": "Goods received recorded.",
    "cancelled": "Cancelled.",
}


@router.get("/finance/purchase-orders/new", response_class=HTMLResponse)
@requires("finance.purchase.create")
@screen("finance.purchase_order")
def new_order(request: Request, db: Db, actor: CurrentActor, supplier: str = "") -> HTMLResponse:
    context = _order_context(db, actor, None, values={"supplier_id": supplier})
    return _render(request, "finance/purchase_order.html", context)


@router.get("/finance/purchase-orders/{order_id:uuid}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.purchase_order")
def order_page(
    request: Request, db: Db, actor: CurrentActor, order_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _order_context(db, actor, order_id, notice=ORDER_NOTICES.get(notice))
    return _render(request, "finance/purchase_order.html", context)


@router.post("/finance/purchase-orders", response_class=HTMLResponse)
@requires("finance.purchase.create")
@screen("finance.purchase_order")
def save_order(
    request: Request,
    db: Db,
    actor: CurrentActor,
    order_id: FormStr = "",
    supplier_id: FormStr = "",
    order_date: FormStr = "",
    expected_date: FormStr = "",
    reference: FormStr = "",
    memo: FormStr = "",
    description: FormList = [],  # noqa: B006  (FastAPI form lists)
    account_id: FormList = [],  # noqa: B006
    cost_centre_id: FormList = [],  # noqa: B006
    quantity: FormList = [],  # noqa: B006
    unit_price: FormList = [],  # noqa: B006
    vat_code: FormList = [],  # noqa: B006
) -> Response:
    lines = _line_inputs(description, account_id, cost_centre_id, quantity, unit_price, vat_code)
    data = purchasing.OrderInput(
        supplier_id, parse_day(order_date), parse_day(expected_date), reference, memo, lines
    )
    existing = _uuid(order_id)
    try:
        with db.begin_nested():
            saved = purchasing.save_order(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "supplier_id": supplier_id,
            "order_date": order_date,
            "expected_date": expected_date,
            "reference": reference,
            "memo": memo,
        }
        context = _order_context(
            db,
            actor,
            existing,
            lines=[line for line in lines if line.description or line.unit_price],
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/purchase_order.html", context, 422)
    return _see_other(f"/finance/purchase-orders/{saved}?notice=saved")


def _delete_order(db: Session, actor: Actor, order_id: uuid.UUID) -> str:
    purchasing.delete_order(db, actor, order_id)
    return "deleted"


def _order_action(
    request: Request, db: Session, actor: Actor, order_id: uuid.UUID, run: Any, notice: str
) -> Response:
    try:
        with db.begin_nested():
            target = run()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _order_context(db, actor, order_id, errors=_errors(exc))
        return _render(request, "finance/purchase_order.html", context, 422)
    if isinstance(target, uuid.UUID):  # a new bill
        return _see_other(f"/finance/bills/{target}?notice=drafted")
    if target == "deleted":
        return _see_other("/finance/purchases?tab=order&notice=deleted")
    return _see_other(f"/finance/purchase-orders/{order_id}?notice={notice}")


@router.post("/finance/purchase-orders/{order_id:uuid}/receive", response_class=HTMLResponse)
@requires("finance.purchase.create")
@screen("finance.purchase_order")
def receive_goods(
    request: Request,
    db: Db,
    actor: CurrentActor,
    order_id: uuid.UUID,
    received_on: FormStr = "",
    delivery_note: FormStr = "",
    note: FormStr = "",
    line_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    received: FormList = [],  # noqa: B006
) -> Response:
    quantities = dict(zip(line_id, received, strict=False))
    try:
        with db.begin_nested():
            purchasing.receive(
                db,
                actor,
                order_id,
                received_on=parse_day(received_on),
                delivery_note=delivery_note,
                note=note,
                quantities=quantities,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "received_on": received_on,
            "delivery_note": delivery_note,
            "note": note,
            **{f"qty_{k}": v for k, v in quantities.items()},
        }
        context = _order_context(db, actor, order_id, values=values, errors=_errors(exc))
        return _render(request, "finance/purchase_order.html", context, 422)
    return _see_other(f"/finance/purchase-orders/{order_id}?notice=received")


@router.post("/finance/purchase-orders/{order_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.purchase_order")
def order_action(
    request: Request, db: Db, actor: CurrentActor, order_id: uuid.UUID, action: str
) -> Response:
    """approve, cancel, delete or bill: each service checks its own permission."""
    runs = {
        "approve": lambda: purchasing.approve_order(db, actor, order_id),
        "cancel": lambda: purchasing.cancel_order(db, actor, order_id),
        "delete": lambda: _delete_order(db, actor, order_id),
        "bill": lambda: bills.bill_from_order(db, actor, order_id, today=clock.today()),
    }
    if action not in runs:
        return _see_other(f"/finance/purchase-orders/{order_id}")
    notice = {"approve": "approved", "cancel": "cancelled"}.get(action, "")
    return _order_action(request, db, actor, order_id, runs[action], notice)


# ------------------------------------------------------------------ bills and debit notes


def _bill_context(
    db: Session, actor: Actor, bill_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    today = clock.today()
    view = bills.get_bill(db, actor, bill_id, today=today) if bill_id else None
    lines = extra.pop("lines", None)
    if lines is None:
        lines = [
            BillLineInput(
                str(line.order_line_id or ""),
                line.description,
                str(line.account_id),
                str(line.cost_centre_id or ""),
                f"{line.quantity.normalize():f}",
                f"{line.unit_price:.2f}",
                line.vat_code,
            )
            for line in (view.lines if view else [])
        ]
    blank = BillLineInput("", "", "", "", "", "", "standard")
    from_order = bool(view and view.order and view.kind == "bill")
    return {
        "shell": _shell(db, actor, "Purchases"),
        "bill": view,
        "kind": view.kind if view else extra.pop("kind", "bill"),
        "rows": lines if from_order else _form_rows(lines, blank),
        "from_order": from_order,
        "suppliers": suppliers.choices(db),
        "wht_types": tax.wht_types(db, active_only=True),
        "status_labels": bills.STATUS_LABELS,
        "tones": bills.STATUS_TONES,
        "kind_labels": bills.KIND_LABELS,
        "tolerance": periods.settings(db).price_tolerance,
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (view.number or f"Draft {bills.KIND_LABELS[view.kind].lower()}")
        if view
        else "New bill",
        **_pickers(db),
        **extra,
    }


BILL_NOTICES = {
    "saved": "Draft saved. Someone other than you approves it.",
    "drafted": "Bill drafted from what was received. Check it against the supplier's invoice.",
    "posted": "Approved and posted.",
    "held": "Held: it doesn't match the purchase order or what was received.",
    "note": "Debit note drafted. Someone else issues it.",
}


@router.get("/finance/bills/new", response_class=HTMLResponse)
@requires("finance.purchase.create")
@screen("finance.bill")
def new_bill(request: Request, db: Db, actor: CurrentActor, supplier: str = "") -> HTMLResponse:
    context = _bill_context(db, actor, None, values={"supplier_id": supplier})
    return _render(request, "finance/bill.html", context)


@router.get("/finance/bills/{bill_id:uuid}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.bill")
def bill_page(
    request: Request, db: Db, actor: CurrentActor, bill_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _bill_context(db, actor, bill_id, notice=BILL_NOTICES.get(notice))
    return _render(request, "finance/bill.html", context)


@router.post("/finance/bills", response_class=HTMLResponse)
@requires("finance.purchase.create")
@screen("finance.bill")
def save_bill(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bill_id: FormStr = "",
    kind: FormStr = "bill",
    supplier_id: FormStr = "",
    supplier_reference: FormStr = "",
    bill_date: FormStr = "",
    due_date: FormStr = "",
    wht_type_id: FormStr = "",
    memo: FormStr = "",
    order_line_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    description: FormList = [],  # noqa: B006
    account_id: FormList = [],  # noqa: B006
    cost_centre_id: FormList = [],  # noqa: B006
    quantity: FormList = [],  # noqa: B006
    unit_price: FormList = [],  # noqa: B006
    vat_code: FormList = [],  # noqa: B006
) -> Response:
    rows = max(len(description), len(account_id), len(unit_price))
    lines = [
        BillLineInput(
            _at(order_line_id, i),
            _at(description, i),
            _at(account_id, i),
            _at(cost_centre_id, i),
            _at(quantity, i),
            _at(unit_price, i),
            _at(vat_code, i) or "standard",
        )
        for i in range(rows)
    ]
    data = BillInput(
        kind,
        supplier_id,
        supplier_reference,
        parse_day(bill_date),
        parse_day(due_date),
        wht_type_id,
        memo,
        lines,
    )
    existing = _uuid(bill_id)
    try:
        with db.begin_nested():
            saved = bills.save_bill(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "supplier_id": supplier_id,
            "supplier_reference": supplier_reference,
            "bill_date": bill_date,
            "due_date": due_date,
            "wht_type_id": wht_type_id,
            "memo": memo,
        }
        context = _bill_context(
            db,
            actor,
            existing,
            kind=kind if kind in bills.KIND_LABELS else "bill",
            lines=[line for line in lines if line.description or line.unit_price],
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/bill.html", context, 422)
    return _see_other(f"/finance/bills/{saved}?notice=saved")


@router.post("/finance/bills/{bill_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.bill")
def bill_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bill_id: uuid.UUID,
    action: str,
    reason: FormStr = "",
) -> Response:
    """approve, delete or debit-note: each service checks its own permission."""
    try:
        with db.begin_nested():
            if action == "approve":
                status = bills.approve(db, actor, bill_id, reason=reason)
                target = f"/finance/bills/{bill_id}?notice={status}"
            elif action == "delete":
                kind = bills.delete_bill(db, actor, bill_id)
                target = f"/finance/purchases?tab={kind}&notice=deleted"
            elif action == "debit-note":
                note = bills.debit_note_for(db, actor, bill_id, today=clock.today())
                target = f"/finance/bills/{note}?notice=note"
            else:
                target = f"/finance/bills/{bill_id}"
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _bill_context(db, actor, bill_id, values={"reason": reason}, errors=_errors(exc))
        return _render(request, "finance/bill.html", context, 422)
    return _see_other(target)


# ------------------------------------------------------------------ payments


def _payment_context(
    db: Session, actor: Actor, payment_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = payments.get_payment(db, actor, payment_id) if payment_id else None
    values = extra.get("values", {})
    supplier_id = view.supplier_id if view else _uuid(values.get("supplier_id"))
    today = clock.today()
    supplier = db.get(Supplier, supplier_id) if supplier_id else None
    open_ = payables.open_bills(db, supplier_id, as_at=today) if supplier_id else []
    types = {t.id: t for t in tax.wht_types(db)}
    rates = {
        b.id: payments.wht_rate(supplier, types.get(b.wht_type_id) if b.wht_type_id else None)
        for b in open_
        if supplier
    }
    allocations = extra.pop("allocations", None)
    if allocations is None:
        allocations = payments.draft_values(db, view.id) if view and view.status == "draft" else {}
    return {
        "shell": _shell(db, actor, "Purchases"),
        "payment": view,
        "suppliers": suppliers.choices(db),
        "open_bills": open_,
        "wht_names": {t.id: t.name for t in types.values()},
        "rates": rates,
        "banks": receipts.bank_accounts(db),
        "allocations": allocations,
        "status_labels": payments.STATUS_LABELS,
        "tones": payments.STATUS_TONES,
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (view.number or "Payment awaiting approval") if view else "Prepare payment",
        **extra,
    }


PAYMENT_NOTICES = {
    "saved": "Saved for approval. Someone other than you approves it.",
    "approved": "Approved and posted. Download the bank file to pay.",
    "reversed": "Payment reversed. The bills it paid are open again.",
}


@router.get("/finance/payments/new", response_class=HTMLResponse)
@requires("finance.payment.create")
@screen("finance.payment")
def new_payment(request: Request, db: Db, actor: CurrentActor, supplier: str = "") -> HTMLResponse:
    context = _payment_context(db, actor, None, values={"supplier_id": supplier})
    return _render(request, "finance/payment.html", context)


@router.get("/finance/payments/{payment_id:uuid}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.payment")
def payment_page(
    request: Request, db: Db, actor: CurrentActor, payment_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _payment_context(db, actor, payment_id, notice=PAYMENT_NOTICES.get(notice))
    return _render(request, "finance/payment.html", context)


@router.post("/finance/payments", response_class=HTMLResponse)
@requires("finance.payment.create")
@screen("finance.payment")
def save_payment(
    request: Request,
    db: Db,
    actor: CurrentActor,
    payment_id: FormStr = "",
    supplier_id: FormStr = "",
    payment_date: FormStr = "",
    bank_account_id: FormStr = "",
    reference: FormStr = "",
    bill_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    pay: FormList = [],  # noqa: B006
    step: FormStr = "",
) -> Response:
    values = {
        "supplier_id": supplier_id,
        "payment_date": payment_date,
        "bank_account_id": bank_account_id,
        "reference": reference,
    }
    pairs = list(zip(bill_id, pay, strict=False))
    existing = _uuid(payment_id)
    if step == "supplier":  # the supplier changed: show their open bills
        return _render(
            request, "finance/payment.html", _payment_context(db, actor, None, values=values)
        )
    data = payments.PaymentInput(
        supplier_id, parse_day(payment_date), bank_account_id, reference, pairs
    )
    try:
        with db.begin_nested():
            saved = payments.prepare(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _payment_context(
            db,
            actor,
            existing,
            values=values,
            allocations=dict(pairs),
            errors=_errors(exc),
        )
        return _render(request, "finance/payment.html", context, 422)
    return _see_other(f"/finance/payments/{saved}?notice=saved")


@router.post("/finance/payments/{payment_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.purchase.read")
@screen("finance.payment")
def payment_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    payment_id: uuid.UUID,
    action: str,
    reverse_on: FormStr = "",
    reason: FormStr = "",
) -> Response:
    """approve, delete or reverse: each service checks its own permission."""
    try:
        with db.begin_nested():
            if action == "approve":
                payments.approve(db, actor, payment_id)
                target = f"/finance/payments/{payment_id}?notice=approved"
            elif action == "delete":
                payments.delete(db, actor, payment_id)
                target = "/finance/purchases?tab=payment&notice=deleted"
            elif action == "reverse":
                payments.reverse(db, actor, payment_id, on=parse_day(reverse_on), reason=reason)
                target = f"/finance/payments/{payment_id}?notice=reversed"
            else:
                target = f"/finance/payments/{payment_id}"
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _payment_context(
            db,
            actor,
            payment_id,
            values={"reverse_on": reverse_on, "reason": reason},
            errors=_errors(exc),
        )
        return _render(request, "finance/payment.html", context, 422)
    return _see_other(target)


@router.get("/finance/payments/{payment_id:uuid}/bank.csv")
@requires("finance.payment.approve")
def payment_bank_file(db: Db, actor: CurrentActor, payment_id: uuid.UUID) -> Response:
    name, text = payments.bank_file(db, actor, payment_id)
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ------------------------------------------------------------------ aging


@router.get("/finance/payables-aging", response_class=HTMLResponse)
@requires("finance.supplier.read")
@screen("finance.payables_aging")
def aging_page(request: Request, db: Db, actor: CurrentActor, as_at: str = "") -> HTMLResponse:
    at = parse_day(as_at) or clock.today()
    context = {
        "shell": _shell(db, actor, "Payables aging"),
        "report": payables.aging(db, as_at=at),
        "buckets": receivables.BUCKETS,
        "as_at": at,
        "page_title": "Payables aging",
    }
    return _render(request, "finance/payables_aging.html", context)


@router.get("/finance/payables-aging.csv")
@requires("finance.supplier.read")
def aging_csv(db: Db, actor: CurrentActor, as_at: str = "") -> Response:
    at: date = parse_day(as_at) or clock.today()
    report = payables.aging(db, as_at=at)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["Code", "Supplier", *receivables.BUCKETS, "Total"])
    for row in report.rows:
        name = "'" + row.name if row.name[:1] in {"=", "+", "-", "@"} else row.name
        writer.writerow([row.code, name, *(f"{b:.2f}" for b in row.buckets), f"{row.total:.2f}"])
    writer.writerow(["", "Total", *(f"{b:.2f}" for b in report.totals), f"{report.total:.2f}"])
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="payables-aging-{at}.csv"'},
    )
