"""Inventory web routes (ADR 0028): items, setup, movements and counts, and material
requests. Routes translate forms and render; the services do the work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.inventory import counts, documents, items, requests, stock
from app.modules.inventory.documents import DocumentInput, LineInput
from app.modules.inventory.items import CategoryInput, ItemInput, can, uuid_or_none
from app.modules.inventory.models import Item, ItemCategory, Warehouse
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import all_fields, parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
Fields = Annotated[dict[str, str], Depends(all_fields)]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
BLANK_ROWS = 3
MOVEMENT_TABS = (
    ("issue", "Issues"),
    ("transfer", "Transfers"),
    ("adjustment", "Adjustments"),
    ("waiting", "Waiting to post"),
    ("counts", "Counts"),
)
REQUEST_TABS = (("mine", "Mine"), ("approve", "To approve"), ("issue", "To issue"), ("all", "All"))
NOTICES = {
    "saved": "Saved.",
    "posted": "Posted.",
    "submitted": "Submitted. Someone other than you posts it.",
    "returned": "Sent back to draft.",
    "deleted": "Deleted.",
    "counted": "Counts saved.",
    "approved": "Approved. Inventory staff issue it next.",
    "rejected": "Rejected.",
    "issued": "Issued.",
    "cancelled": "Cancelled.",
    "request_submitted": "Sent to your line manager.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="inventory", active_section=section, crumbs=["Inventory", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _at(values: list[str], i: int) -> str:
    return values[i] if i < len(values) else ""


@router.get("/inventory", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.items")
def inventory_home(request: Request, actor: CurrentActor) -> Response:
    return _see_other("/inventory/items")


# ------------------------------------------------------------------ items


@router.get("/inventory/items", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.items")
def item_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category: str = "",
    warehouse: str = "",
    view: str = "all",
    q: str = "",
) -> HTMLResponse:
    view = view if view in ("all", "low", "inactive") else "all"
    names = {w.id: w.name for w in items.warehouses(db)}
    by_id = {i.id: i for i in items.item_choices(db)}
    low = [
        (by_id[i], names.get(w, ""), stock.on_hand(db, i, w))
        for i, w in items.low_places(db)
        if i in by_id
    ]
    context = {
        "shell": _shell(db, actor, "Items"),
        "rows": items.item_rows(db, actor, category=category, warehouse=warehouse, view=view, q=q),
        "totals": items.totals(db),
        "low": low,
        "categories": items.categories(db),
        "warehouses": items.warehouses(db),
        "filters": {"category": category, "warehouse": warehouse, "view": view, "q": q},
        "valuations": items.VALUATION_LABELS,
        "trackings": items.TRACKING_LABELS,
        "can_manage": can(db, actor, items.MANAGE),
        "can_move": can(db, actor, documents.MOVE),
        "page_title": "Items",
    }
    return _render(request, "inventory/items.html", context)


def _item_context(
    db: Session, actor: Actor, item_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = items.item_view(db, actor, item_id) if item_id else None
    return {
        "shell": _shell(db, actor, "Items"),
        "v": view,
        "categories": items.categories(db, active_only=True),
        "units": items.units(db, active_only=True),
        "valuations": items.VALUATION_LABELS,
        "trackings": items.TRACKING_LABELS,
        "kinds": {
            "issue": "Issue",
            "transfer_out": "Transfer out",
            "transfer_in": "Transfer in",
            "adjustment_in": "Adjustment in",
            "adjustment_out": "Adjustment out",
            "count_in": "Count gain",
            "count_out": "Count loss",
            "receipt": "Goods received",
            "purchase_return": "Returned to supplier",
            "sale": "Sold",
            "sale_return": "Returned by customer",
        },
        "can_manage": can(db, actor, items.MANAGE),
        "editing": extra.pop("editing", view is None),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{view.item.code} {view.item.name}" if view else "Add item",
        **extra,
    }


@router.get("/inventory/items/new", response_class=HTMLResponse)
@requires("inventory.item.manage")
@screen("inventory.item")
def new_item(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "inventory/item.html", _item_context(db, actor, None))


@router.get("/inventory/items/{item_id:uuid}", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.item")
def item_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    item_id: uuid.UUID,
    edit: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _item_context(db, actor, item_id, editing=bool(edit), notice=NOTICES.get(notice))
    return _render(request, "inventory/item.html", context)


@router.post("/inventory/items/new", response_class=HTMLResponse)
@router.post("/inventory/items/{item_id:uuid}", response_class=HTMLResponse)
@requires("inventory.item.manage")
@screen("inventory.item")
def save_item(
    request: Request,
    db: Db,
    actor: CurrentActor,
    item_id: uuid.UUID | None = None,
    code: FormStr = "",
    name: FormStr = "",
    category_id: FormStr = "",
    unit_id: FormStr = "",
    valuation: FormStr = "average",
    tracking: FormStr = "none",
    reorder_level: FormStr = "",
    reorder_quantity: FormStr = "",
    description: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = ItemInput(
        code,
        name,
        category_id,
        unit_id,
        valuation,
        tracking,
        reorder_level,
        reorder_quantity,
        description,
        bool(is_active) or item_id is None,
    )
    try:
        with db.begin_nested():
            saved = items.save_item(db, actor, item_id, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "code": code,
            "name": name,
            "category_id": category_id,
            "unit_id": unit_id,
            "valuation": valuation,
            "tracking": tracking,
            "reorder_level": reorder_level,
            "reorder_quantity": reorder_quantity,
            "description": description,
        }
        context = _item_context(
            db, actor, item_id, editing=True, values=values, errors=_errors(exc)
        )
        return _render(request, "inventory/item.html", context, 422)
    return _see_other(f"/inventory/items/{saved}?notice=saved")


# ------------------------------------------------------------------ setup


def _setup_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Setup"),
        "warehouses": items.warehouses(db),
        "bins": items.bins_by_warehouse(db),
        "categories": items.categories(db),
        "units": items.units(db),
        "accounts": {a.id: f"{a.code} {a.name}" for a in finance.all_accounts(db)},
        "postable": finance.postable(db),
        "can_manage": can(db, actor, items.MANAGE),
        "category": None,
        "warehouse": None,
        "form": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Inventory setup",
        **extra,
    }


@router.get("/inventory/setup", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.setup")
def setup(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category: str = "",
    warehouse: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _setup_context(
        db,
        actor,
        category=db.get(ItemCategory, uuid_or_none(category)) if uuid_or_none(category) else None,
        warehouse=db.get(Warehouse, uuid_or_none(warehouse)) if uuid_or_none(warehouse) else None,
        form="category" if category == "new" else ("warehouse" if warehouse == "new" else ""),
        notice=NOTICES.get(notice),
    )
    return _render(request, "inventory/setup.html", context)


@router.post("/inventory/setup/{what}", response_class=HTMLResponse)
@requires("inventory.item.manage")
@screen("inventory.setup")
def save_setup(
    request: Request,
    db: Db,
    actor: CurrentActor,
    what: str,
    record_id: FormStr = "",
    code: FormStr = "",
    name: FormStr = "",
    address: FormStr = "",
    warehouse_id: FormStr = "",
    inventory_account_id: FormStr = "",
    cogs_account_id: FormStr = "",
    adjustment_account_id: FormStr = "",
    expense_account_id: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    existing = uuid_or_none(record_id)
    active = bool(is_active) or existing is None
    try:
        with db.begin_nested():
            if what == "unit":
                items.save_unit(db, actor, existing, code=code, name=name, active=active)
            elif what == "category":
                items.save_category(
                    db,
                    actor,
                    existing,
                    CategoryInput(
                        name,
                        inventory_account_id,
                        cogs_account_id,
                        adjustment_account_id,
                        expense_account_id,
                        active,
                    ),
                )
            elif what == "warehouse":
                items.save_warehouse(
                    db, actor, existing, code=code, name=name, address=address, active=active
                )
            elif what == "bin":
                items.add_bin(db, actor, uuid_or_none(warehouse_id) or uuid.uuid4(), code)
            else:
                raise Conflict("Choose what to save.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "code": code,
            "name": name,
            "address": address,
            "warehouse_id": warehouse_id,
            "inventory_account_id": inventory_account_id,
            "cogs_account_id": cogs_account_id,
            "adjustment_account_id": adjustment_account_id,
            "expense_account_id": expense_account_id,
        }
        context = _setup_context(
            db,
            actor,
            category=db.get(ItemCategory, existing) if what == "category" and existing else None,
            warehouse=db.get(Warehouse, existing) if what == "warehouse" and existing else None,
            form=what,
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "inventory/setup.html", context, 422)
    return _see_other("/inventory/setup?notice=saved")


# ------------------------------------------------------------------ movements and counts


@router.get("/inventory/movements", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.movements")
def movement_list(
    request: Request, db: Db, actor: CurrentActor, tab: str = "issue", notice: str = ""
) -> HTMLResponse:
    tab = tab if tab in dict(MOVEMENT_TABS) else "issue"
    context = {
        "shell": _shell(db, actor, "Counts" if tab == "counts" else "Movements"),
        "tab": tab,
        "tabs": MOVEMENT_TABS,
        "rows": documents.list_documents(db, actor, tab) if tab != "counts" else [],
        "counts": counts.list_counts(db, actor) if tab == "counts" else [],
        "kinds": documents.KIND_LABELS,
        "labels": documents.STATUS_LABELS,
        "tones": documents.STATUS_TONES,
        "count_labels": counts.STATUS_LABELS,
        "count_tones": counts.STATUS_TONES,
        "warehouses": items.warehouses(db, active_only=True),
        "can_move": can(db, actor, documents.MOVE),
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": NOTICES.get(notice),
        "page_title": "Stock counts" if tab == "counts" else "Stock movements",
    }
    return _render(request, "inventory/movements.html", context)


@router.get("/inventory/counts", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.movements")
def count_list(request: Request, actor: CurrentActor) -> Response:
    return _see_other("/inventory/movements?tab=counts")


def _document_context(
    db: Session, actor: Actor, document_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = documents.get(db, actor, document_id) if document_id else None
    kind = view.doc.kind if view else extra.pop("kind", "issue")
    rows = extra.pop("rows", None)
    if rows is None:
        rows = [
            {
                "item_id": str(line.item_id),
                "quantity": f"{line.quantity.normalize():f}",
                "unit_cost": f"{line.unit_cost.normalize():f}"
                if line.unit_cost is not None
                else "",
                "bin_id": str(line.bin_id or ""),
                "to_bin_id": str(line.to_bin_id or ""),
                "lot_number": line.lot_number or "",
                "expiry": line.expiry.isoformat() if line.expiry else "",
                "serials": line.serials or "",
            }
            for line in (view.lines if view else [])
        ]
    editable = view is None or view.can_edit
    if editable:
        rows = rows + [{}] * BLANK_ROWS
    header = extra.pop("header", None) or (
        {
            "doc_date": view.doc.doc_date.isoformat(),
            "warehouse_id": str(view.doc.warehouse_id),
            "to_warehouse_id": str(view.doc.to_warehouse_id or ""),
            "cost_centre_id": str(view.doc.cost_centre_id or ""),
            "account_id": str(view.doc.account_id or ""),
            "reason": view.doc.reason or "",
            "project_id": str(view.doc.project_id or ""),
        }
        if view
        else {"doc_date": clock.today().isoformat()}
    )
    return {
        "shell": _shell(db, actor, "Movements"),
        "projects": finance.project_link().choices(db),
        "v": view,
        "kind": kind,
        "kinds": documents.KIND_LABELS,
        "labels": documents.STATUS_LABELS,
        "tones": documents.STATUS_TONES,
        "rows": rows,
        "header": header,
        "editable": editable,
        "items": items.item_choices(db),
        "all_items": {i.id: i for i in db.scalars(select(Item))},
        "warehouses": items.warehouses(db, active_only=True),
        "warehouse_names": {w.id: w.name for w in items.warehouses(db)},
        "bins": items.bins_by_warehouse(db),
        "bin_codes": {
            b.id: b.code for group in items.bins_by_warehouse(db).values() for b in group
        },
        "cost_centres": org.cost_centre_choices(db),
        "postable": finance.postable(db),
        "accounts": {a.id: f"{a.code} {a.name}" for a in finance.all_accounts(db)},
        "errors": {},
        "notice": None,
        "page_title": (
            f"{documents.KIND_LABELS[kind]} {view.doc.number}"
            if view and view.doc.number
            else f"Draft {documents.KIND_LABELS[kind].lower()}"
        ),
        **extra,
    }


@router.get("/inventory/movements/new", response_class=HTMLResponse)
@requires("inventory.stock.move")
@screen("inventory.movement")
def new_movement(
    request: Request, db: Db, actor: CurrentActor, kind: str = "issue"
) -> HTMLResponse:
    kind = kind if kind in documents.KIND_LABELS else "issue"
    return _render(
        request, "inventory/movement.html", _document_context(db, actor, None, kind=kind)
    )


@router.get("/inventory/movements/{document_id:uuid}", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.movement")
def movement_page(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _document_context(db, actor, document_id, notice=NOTICES.get(notice))
    return _render(request, "inventory/movement.html", context)


@router.post("/inventory/movements/new", response_class=HTMLResponse)
@router.post("/inventory/movements/{document_id:uuid}", response_class=HTMLResponse)
@requires("inventory.stock.move")
@screen("inventory.movement")
def save_movement(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: uuid.UUID | None = None,
    kind: FormStr = "issue",
    doc_date: FormStr = "",
    warehouse_id: FormStr = "",
    to_warehouse_id: FormStr = "",
    cost_centre_id: FormStr = "",
    account_id: FormStr = "",
    reason: FormStr = "",
    project_id: FormStr = "",
    item_id: FormList = [],  # noqa: B006  (FastAPI form default)
    quantity: FormList = [],  # noqa: B006
    unit_cost: FormList = [],  # noqa: B006
    bin_id: FormList = [],  # noqa: B006
    to_bin_id: FormList = [],  # noqa: B006
    lot_number: FormList = [],  # noqa: B006
    expiry: FormList = [],  # noqa: B006
    serials: FormList = [],  # noqa: B006
    then: FormStr = "",
) -> Response:
    count = max(len(item_id), len(quantity))
    lines = tuple(
        LineInput(
            _at(item_id, i),
            _at(quantity, i),
            _at(unit_cost, i),
            _at(bin_id, i),
            _at(to_bin_id, i),
            _at(lot_number, i),
            parse_day(_at(expiry, i)),
            _at(serials, i),
        )
        for i in range(count)
    )
    data = DocumentInput(
        kind,
        parse_day(doc_date),
        warehouse_id,
        to_warehouse_id,
        cost_centre_id,
        account_id,
        reason,
        lines,
        project_id,
    )
    try:
        with db.begin_nested():
            saved = documents.save(db, actor, document_id, data)
            if then == "post":
                documents.post(db, actor, saved)
            elif then == "submit":
                documents.submit(db, actor, saved)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        rows = [
            {
                "item_id": x.item_id,
                "quantity": x.quantity,
                "unit_cost": x.unit_cost,
                "bin_id": x.bin_id,
                "to_bin_id": x.to_bin_id,
                "lot_number": x.lot_number,
                "expiry": x.expiry.isoformat() if x.expiry else "",
                "serials": x.serials,
            }
            for x in lines
            if x.item_id or x.quantity.strip()
        ]
        header = {
            "doc_date": doc_date,
            "warehouse_id": warehouse_id,
            "to_warehouse_id": to_warehouse_id,
            "cost_centre_id": cost_centre_id,
            "account_id": account_id,
            "reason": reason,
            "project_id": project_id,
        }
        context = _document_context(
            db, actor, document_id, kind=kind, rows=rows, header=header, errors=_errors(exc)
        )
        return _render(request, "inventory/movement.html", context, 422)
    notice = {"post": "posted", "submit": "submitted"}.get(then, "saved")
    return _see_other(f"/inventory/movements/{saved}?notice={notice}")


@router.post("/inventory/movements/{document_id:uuid}/{action}", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.movement")
def movement_action(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, action: str
) -> Response:
    """post, submit, send-back or delete: each service checks its own permission."""
    runs = {
        "post": (documents.post, "posted"),
        "submit": (documents.submit, "submitted"),
        "send-back": (documents.send_back, "returned"),
        "delete": (documents.delete, "deleted"),
    }
    if action not in runs:
        return _see_other(f"/inventory/movements/{document_id}")
    run, notice = runs[action]
    try:
        with db.begin_nested():
            run(db, actor, document_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _document_context(db, actor, document_id, errors=_errors(exc))
        return _render(request, "inventory/movement.html", context, 422)
    if action == "delete":
        return _see_other("/inventory/movements?notice=deleted")
    return _see_other(f"/inventory/movements/{document_id}?notice={notice}")


@router.post("/inventory/counts", response_class=HTMLResponse)
@requires("inventory.stock.move")
@screen("inventory.movements")
def start_count(
    request: Request,
    db: Db,
    actor: CurrentActor,
    warehouse_id: FormStr = "",
    on: FormStr = "",
    note: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            count_id = counts.start(
                db, actor, warehouse_id=warehouse_id, on=parse_day(on), note=note
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = {
            "shell": _shell(db, actor, "Counts"),
            "tab": "counts",
            "tabs": MOVEMENT_TABS,
            "rows": [],
            "counts": counts.list_counts(db, actor),
            "kinds": documents.KIND_LABELS,
            "labels": documents.STATUS_LABELS,
            "tones": documents.STATUS_TONES,
            "count_labels": counts.STATUS_LABELS,
            "count_tones": counts.STATUS_TONES,
            "warehouses": items.warehouses(db, active_only=True),
            "can_move": can(db, actor, documents.MOVE),
            "today": clock.today(),
            "values": {"warehouse_id": warehouse_id, "on": on, "note": note},
            "errors": _errors(exc),
            "notice": None,
            "page_title": "Stock counts",
        }
        return _render(request, "inventory/movements.html", context, 422)
    return _see_other(f"/inventory/counts/{count_id}")


def _count_context(db: Session, actor: Actor, count_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    view = counts.get(db, actor, count_id)
    return {
        "shell": _shell(db, actor, "Counts"),
        "v": view,
        "labels": counts.STATUS_LABELS,
        "tones": counts.STATUS_TONES,
        "items": items.item_choices(db),
        "bins": items.bins_by_warehouse(db).get(view.count.warehouse_id, []),
        "only_differences": False,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"Count {view.count.number}",
        **extra,
    }


@router.get("/inventory/counts/{count_id:uuid}", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.count")
def count_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    count_id: uuid.UUID,
    notice: str = "",
    differences: str = "",
) -> HTMLResponse:
    context = _count_context(
        db, actor, count_id, notice=NOTICES.get(notice), only_differences=bool(differences)
    )
    return _render(request, "inventory/count.html", context)


@router.post("/inventory/counts/{count_id:uuid}/{action}", response_class=HTMLResponse)
@requires("inventory.item.read")
@screen("inventory.count")
def count_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    count_id: uuid.UUID,
    action: str,
    form: Fields,
) -> Response:
    """save (counts), add (a line), submit, send-back, post or delete."""
    notice = {
        "save": "counted",
        "add": "saved",
        "submit": "submitted",
        "send-back": "returned",
        "post": "posted",
        "delete": "deleted",
    }.get(action)
    try:
        with db.begin_nested():
            if action in ("save", "submit"):
                entered = {k[2:]: v for k, v in form.items() if k.startswith("c_")}
                if entered:
                    counts.save_counts(db, actor, count_id, entered)
                if action == "submit":
                    counts.submit(db, actor, count_id)
            elif action == "add":
                counts.add_line(
                    db,
                    actor,
                    count_id,
                    item_id=form.get("item_id", ""),
                    bin_id=form.get("bin_id", ""),
                    lot_number=form.get("lot_number", ""),
                )
            elif action == "send-back":
                counts.send_back(db, actor, count_id)
            elif action == "post":
                counts.post(db, actor, count_id)
            elif action == "delete":
                counts.delete(db, actor, count_id)
            else:
                raise Conflict("Choose what to do with the count.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _count_context(db, actor, count_id, values=form, errors=_errors(exc))
        return _render(request, "inventory/count.html", context, 422)
    if action == "delete":
        return _see_other("/inventory/movements?tab=counts&notice=deleted")
    return _see_other(f"/inventory/counts/{count_id}?notice={notice}")


# ------------------------------------------------------------------ material requests


@router.get("/inventory/requests", response_class=HTMLResponse)
@requires("inventory.request.read")
@screen("inventory.requests")
def request_list(
    request: Request, db: Db, actor: CurrentActor, tab: str = "mine", notice: str = ""
) -> HTMLResponse:
    tab = tab if tab in dict(REQUEST_TABS) else "mine"
    context = {
        "shell": _shell(db, actor, "Material requests"),
        "tab": tab,
        "tabs": REQUEST_TABS,
        "rows": requests.list_requests(db, actor, tab),
        "counts": requests.counts(db, actor),
        "labels": requests.STATUS_LABELS,
        "tones": requests.STATUS_TONES,
        "can_create": can(db, actor, requests.CREATE),
        "notice": NOTICES.get(notice),
        "page_title": "Material requests",
    }
    return _render(request, "inventory/requests.html", context)


def _request_context(
    db: Session, actor: Actor, request_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = requests.get(db, actor, request_id) if request_id else None
    rows = extra.pop("rows", None)
    if rows is None:
        rows = [
            {"item_id": str(x.item_id), "quantity": f"{x.quantity.normalize():f}"}
            for x in (view.lines if view else [])
        ]
    if view is None or view.can_edit:
        rows = rows + [{}] * BLANK_ROWS
    header = extra.pop("header", None) or (
        {
            "warehouse_id": str(view.request.warehouse_id),
            "needed_by": view.request.needed_by.isoformat() if view.request.needed_by else "",
            "purpose": view.request.purpose,
        }
        if view
        else {}
    )
    return {
        "shell": _shell(db, actor, "Material requests"),
        "v": view,
        "rows": rows,
        "header": header,
        "items": items.item_choices(db),
        "warehouses": items.warehouses(db, active_only=True),
        "postable": [a for a in finance.postable(db) if a.type == "expense"],
        "labels": requests.STATUS_LABELS,
        "tones": requests.STATUS_TONES,
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"Material request {view.request.number}"
        if view and view.request.number
        else "New material request",
        **extra,
    }


@router.get("/inventory/requests/new", response_class=HTMLResponse)
@requires("inventory.request.create")
@screen("inventory.request")
def new_request(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "inventory/request.html", _request_context(db, actor, None))


@router.get("/inventory/requests/{request_id:uuid}", response_class=HTMLResponse)
@requires("inventory.request.read")
@screen("inventory.request")
def request_page(
    request: Request, db: Db, actor: CurrentActor, request_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _request_context(db, actor, request_id, notice=NOTICES.get(notice))
    return _render(request, "inventory/request.html", context)


@router.post("/inventory/requests/new", response_class=HTMLResponse)
@router.post("/inventory/requests/{request_id:uuid}", response_class=HTMLResponse)
@requires("inventory.request.create")
@screen("inventory.request")
def save_request(
    request: Request,
    db: Db,
    actor: CurrentActor,
    request_id: uuid.UUID | None = None,
    warehouse_id: FormStr = "",
    needed_by: FormStr = "",
    purpose: FormStr = "",
    item_id: FormList = [],  # noqa: B006  (FastAPI form default)
    quantity: FormList = [],  # noqa: B006
    then: FormStr = "",
) -> Response:
    pairs = tuple(
        (_at(item_id, i), _at(quantity, i)) for i in range(max(len(item_id), len(quantity)))
    )
    data = requests.RequestInput(warehouse_id, parse_day(needed_by), purpose, pairs)
    try:
        with db.begin_nested():
            saved = requests.save(db, actor, request_id, data)
            if then == "submit":
                requests.submit(db, actor, saved)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        rows = [{"item_id": i, "quantity": q} for i, q in pairs if i or q.strip()]
        header = {"warehouse_id": warehouse_id, "needed_by": needed_by, "purpose": purpose}
        context = _request_context(
            db, actor, request_id, rows=rows, header=header, errors=_errors(exc)
        )
        return _render(request, "inventory/request.html", context, 422)
    notice = "request_submitted" if then == "submit" else "saved"
    return _see_other(f"/inventory/requests/{saved}?notice={notice}")


@router.post("/inventory/requests/{request_id:uuid}/{action}", response_class=HTMLResponse)
@requires("inventory.request.read")
@screen("inventory.request")
def request_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    request_id: uuid.UUID,
    action: str,
    form: Fields,
) -> Response:
    """submit, cancel, approve, reject or issue: each service checks its own permission."""
    notices = {
        "submit": "request_submitted",
        "cancel": "cancelled",
        "approve": "approved",
        "reject": "rejected",
        "issue": "issued",
    }
    try:
        with db.begin_nested():
            if action == "submit":
                requests.submit(db, actor, request_id)
            elif action == "cancel":
                requests.cancel(db, actor, request_id)
            elif action in ("approve", "reject"):
                requests.decide(
                    db, actor, request_id, approve=action == "approve", note=form.get("note", "")
                )
            elif action == "issue":
                requests.issue(
                    db,
                    actor,
                    request_id,
                    quantities={k[2:]: v for k, v in form.items() if k.startswith("q_")},
                    account_id=form.get("account_id", ""),
                    on=parse_day(form.get("on", "")),
                )
            else:
                raise Conflict("Choose what to do with the request.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _request_context(db, actor, request_id, values=form, errors=_errors(exc))
        return _render(request, "inventory/request.html", context, 422)
    if action == "cancel" and form.get("was_draft"):
        return _see_other("/inventory/requests?notice=cancelled")
    return _see_other(f"/inventory/requests/{request_id}?notice={notices.get(action, 'saved')}")
