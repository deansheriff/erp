"""Procurement web routes (ADR 0030): requisitions and their approvals, RFQs with quotes and
the award, supplier performance and settings. Routes translate forms and render; the
services do the work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.inventory import service as inventory
from app.modules.procurement import performance, requisitions, rfqs
from app.modules.procurement import settings as setup
from app.modules.procurement.files import SUPPLIER_QUOTE
from app.modules.procurement.requisitions import LineInput, RequisitionInput
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
QuoteUpload = Annotated[ReceivedUpload, Depends(upload_body(SUPPLIER_QUOTE.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
BLANK_ROWS = 3
MIN_ROWS = 4
REQUISITION_TABS = (
    ("mine", "Mine"),
    ("approve", "To approve"),
    ("sourcing", "Sourcing"),
    ("all", "All"),
)
RFQ_TABS = (("open", "Open"), ("awarded", "Awarded"), ("ordered", "Ordered"), ("all", "All"))
NOTICES = {
    "saved": "Draft saved.",
    "submitted": "Submitted for approval.",
    "approved": "Approved.",
    "fully": "Approved. Procurement can now ask for quotes.",
    "rejected": "Rejected.",
    "cancelled": "Cancelled.",
    "started": "RFQ started. Email it to the suppliers when it's ready.",
    "invited": "Supplier invited.",
    "removed": "Supplier removed.",
    "quote": "Quote recorded.",
    "declined": "Marked as declined.",
    "document": "Document attached. It's checked for viruses before anyone can open it.",
    "awarded": "Awarded. Make the purchase order when you're ready.",
    "approval": "Awarded, but the quoted total needs more approval before ordering.",
    "ordered": "Draft purchase order made. Someone other than you approves it in Payables.",
    "rated": "Rating saved.",
    "settings": "Settings saved.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db,
        actor,
        active_module="procurement",
        active_section=section,
        crumbs=["Procurement", section],
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _at(values: list[str], i: int) -> str:
    return values[i] if i < len(values) else ""


@router.get("/procurement", response_class=HTMLResponse)
@requires("procurement.requisition.read")
@screen("procurement.requisitions")
def procurement_home(request: Request, actor: CurrentActor) -> Response:
    return _see_other("/procurement/requisitions")


# ------------------------------------------------------------------ requisitions


@router.get("/procurement/requisitions", response_class=HTMLResponse)
@requires("procurement.requisition.read")
@screen("procurement.requisitions")
def requisition_list(
    request: Request, db: Db, actor: CurrentActor, tab: str = "mine", notice: str = ""
) -> HTMLResponse:
    tab = tab if tab in dict(REQUISITION_TABS) else "mine"
    context = {
        "shell": _shell(db, actor, "Requisitions"),
        "tab": tab,
        "tabs": REQUISITION_TABS,
        "rows": requisitions.list_requisitions(db, actor, tab),
        "counts": {"approve": requisitions.to_approve(db, actor)},
        "labels": requisitions.STATUS_LABELS,
        "tones": requisitions.STATUS_TONES,
        "can_create": setup.can(db, actor, requisitions.CREATE),
        "notice": NOTICES.get(notice),
        "page_title": "Purchase requisitions",
    }
    return _render(request, "procurement/requisitions.html", context)


def _requisition_context(
    db: Session, actor: Actor, requisition_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = requisitions.get(db, actor, requisition_id) if requisition_id else None
    lines = extra.pop("lines", None)
    if lines is None:
        lines = [
            LineInput(
                str(x.item_id or ""),
                str(x.warehouse_id or ""),
                x.description,
                str(x.account_id or ""),
                f"{x.quantity.normalize():f}",
                f"{x.estimated_price:.2f}",
            )
            for x in (view.lines if view else [])
        ]
    blank = LineInput("", "", "", "", "", "")
    count = max(len(lines) + BLANK_ROWS, MIN_ROWS)
    return {
        "shell": _shell(db, actor, "Requisitions"),
        "view": view,
        "rows": [*lines, *([blank] * (count - len(lines)))],
        "stock_items": inventory.LINK.items(db),
        "warehouses": inventory.LINK.warehouses(db),
        "accounts": finance.purchase_accounts(db),
        "suppliers": finance.supplier_choices(db),
        "labels": requisitions.STATUS_LABELS,
        "tones": requisitions.STATUS_TONES,
        "rfq_labels": rfqs.STATUS_LABELS,
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (view.row.number or "Draft requisition") if view else "New requisition",
        **extra,
    }


@router.get("/procurement/requisitions/new", response_class=HTMLResponse)
@requires("procurement.requisition.create")
@screen("procurement.requisition")
def new_requisition(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "procurement/requisition.html", _requisition_context(db, actor, None))


@router.get("/procurement/requisitions/{requisition_id:uuid}", response_class=HTMLResponse)
@requires("procurement.requisition.read")
@screen("procurement.requisition")
def requisition_page(
    request: Request, db: Db, actor: CurrentActor, requisition_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _requisition_context(db, actor, requisition_id, notice=NOTICES.get(notice))
    return _render(request, "procurement/requisition.html", context)


@router.post("/procurement/requisitions", response_class=HTMLResponse)
@requires("procurement.requisition.create")
@screen("procurement.requisition")
def save_requisition(
    request: Request,
    db: Db,
    actor: CurrentActor,
    requisition_id: FormStr = "",
    purpose: FormStr = "",
    needed_by: FormStr = "",
    then: FormStr = "save",
    item_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    warehouse_id: FormList = [],  # noqa: B006
    description: FormList = [],  # noqa: B006
    account_id: FormList = [],  # noqa: B006
    quantity: FormList = [],  # noqa: B006
    estimated_price: FormList = [],  # noqa: B006
) -> Response:
    rows = max(len(description), len(item_id), len(estimated_price))
    lines = [
        LineInput(
            _at(item_id, i),
            _at(warehouse_id, i),
            _at(description, i),
            _at(account_id, i),
            _at(quantity, i),
            _at(estimated_price, i),
        )
        for i in range(rows)
    ]
    existing = setup.uuid_or_none(requisition_id)
    try:
        with db.begin_nested():
            saved = requisitions.save(
                db, actor, existing, RequisitionInput(parse_day(needed_by), purpose, tuple(lines))
            )
            if then == "submit":
                requisitions.submit(db, actor, saved)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _requisition_context(
            db,
            actor,
            existing,
            lines=[x for x in lines if x.item_id or x.description or x.estimated_price],
            values={"purpose": purpose, "needed_by": needed_by},
            errors=_errors(exc),
        )
        return _render(request, "procurement/requisition.html", context, 422)
    notice = "submitted" if then == "submit" else "saved"
    return _see_other(f"/procurement/requisitions/{saved}?notice={notice}")


@router.post("/procurement/requisitions/{requisition_id:uuid}/rfq", response_class=HTMLResponse)
@requires("procurement.rfq.manage")
@screen("procurement.requisition")
def start_rfq(
    request: Request,
    db: Db,
    actor: CurrentActor,
    requisition_id: uuid.UUID,
    respond_by: FormStr = "",
    note: FormStr = "",
    supplier_id: FormList = [],  # noqa: B006  (FastAPI form lists)
) -> Response:
    try:
        with db.begin_nested():
            rfq = rfqs.start(
                db,
                actor,
                requisition_id,
                respond_by=parse_day(respond_by),
                note=note,
                supplier_ids=supplier_id,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _requisition_context(
            db,
            actor,
            requisition_id,
            values={"respond_by": respond_by, "note": note, "suppliers": supplier_id},
            errors=_errors(exc),
        )
        return _render(request, "procurement/requisition.html", context, 422)
    return _see_other(f"/procurement/rfqs/{rfq}?notice=started")


@router.post(
    "/procurement/requisitions/{requisition_id:uuid}/{action}", response_class=HTMLResponse
)
@requires("procurement.requisition.read")
@screen("procurement.requisition")
def requisition_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    requisition_id: uuid.UUID,
    action: str,
    note: FormStr = "",
) -> Response:
    """submit, cancel, approve or reject: each service checks its own permission."""
    try:
        with db.begin_nested():
            if action == "submit":
                requisitions.submit(db, actor, requisition_id)
                notice = "submitted"
            elif action == "cancel":
                requisitions.cancel(db, actor, requisition_id)
                return _see_other("/procurement/requisitions?notice=cancelled")
            elif action in ("approve", "reject"):
                status = requisitions.decide(
                    db, actor, requisition_id, approve=action == "approve", note=note
                )
                notice = "fully" if status == "approved" else action + "d"
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _requisition_context(
            db, actor, requisition_id, values={"note": note}, errors=_errors(exc)
        )
        return _render(request, "procurement/requisition.html", context, 422)
    return _see_other(f"/procurement/requisitions/{requisition_id}?notice={notice}")


# ------------------------------------------------------------------ RFQs


@router.get("/procurement/rfqs", response_class=HTMLResponse)
@requires("procurement.rfq.read")
@screen("procurement.rfqs")
def rfq_list(request: Request, db: Db, actor: CurrentActor, tab: str = "open") -> HTMLResponse:
    tab = tab if tab in dict(RFQ_TABS) else "open"
    context = {
        "shell": _shell(db, actor, "RFQs"),
        "tab": tab,
        "tabs": RFQ_TABS,
        "rows": rfqs.list_rfqs(db, actor, tab),
        "labels": rfqs.STATUS_LABELS,
        "tones": rfqs.STATUS_TONES,
        "page_title": "Requests for quotation",
    }
    return _render(request, "procurement/rfqs.html", context)


def _rfq_context(db: Session, actor: Actor, rfq_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    view = rfqs.get(db, actor, rfq_id)
    invited = {s.supplier_id for s in view.suppliers}
    return {
        "shell": _shell(db, actor, "RFQs"),
        "view": view,
        "invitable": [(i, n) for i, n in finance.supplier_choices(db) if i not in invited],
        "accounts": finance.purchase_accounts(db),
        "labels": rfqs.STATUS_LABELS,
        "tones": rfqs.STATUS_TONES,
        "level_labels": setup.LEVEL_LABELS,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": view.row.number,
        **extra,
    }


@router.get("/procurement/rfqs/{rfq_id:uuid}", response_class=HTMLResponse)
@requires("procurement.rfq.read")
@screen("procurement.rfq")
def rfq_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    rfq_id: uuid.UUID,
    notice: str = "",
    emailed: str = "",
    missing: str = "",
) -> HTMLResponse:
    message = NOTICES.get(notice)
    if emailed.isdigit():
        message = f"Emailed to {emailed} supplier{'s' if emailed != '1' else ''}."
        if missing.isdigit() and int(missing):
            message += f" {missing} have no email address: send them the PDF another way."
    context = _rfq_context(db, actor, rfq_id, notice=message)
    return _render(request, "procurement/rfq.html", context)


@router.get("/procurement/rfqs/{rfq_id:uuid}/pdf")
@requires("procurement.rfq.read")
def rfq_pdf(db: Db, actor: CurrentActor, rfq_id: uuid.UUID, supplier: str = "") -> Response:
    name, body = rfqs.pdf_for(db, actor, rfq_id, setup.uuid_or_none(supplier))
    return Response(
        body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


def _rfq_run(
    db: Session, actor: Actor, rfq_id: uuid.UUID, action: str, fields: dict[str, str]
) -> str:
    """Run one RFQ action; returns the notice key."""
    if action == "invite":
        rfqs.invite(db, actor, rfq_id, fields.get("supplier_id", ""))
        return "invited"
    if action == "send":
        sent = rfqs.send(db, actor, rfq_id)
        return f"sent:{sent.emailed}:{len(sent.without_email)}"
    if action == "award":
        return rfqs.award(
            db, actor, rfq_id, fields.get("quote_id", ""), reason=fields.get("reason", "")
        )
    if action == "order":
        accounts = {k[8:]: v for k, v in fields.items() if k.startswith("account_")}
        rfqs.make_order(db, actor, rfq_id, accounts=accounts)
        return "ordered"
    if action == "cancel":
        rfqs.cancel(db, actor, rfq_id)
        return "cancelled"
    raise NotFound()


@router.post("/procurement/rfqs/{rfq_id:uuid}/{action}", response_class=HTMLResponse)
@requires("procurement.rfq.manage")
@screen("procurement.rfq")
def rfq_action(
    request: Request, db: Db, actor: CurrentActor, rfq_id: uuid.UUID, action: str, fields: Fields
) -> Response:
    """invite, send, award, order or cancel."""
    try:
        with db.begin_nested():
            notice = _rfq_run(db, actor, rfq_id, action, fields)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _rfq_context(db, actor, rfq_id, values=fields, errors=_errors(exc))
        return _render(request, "procurement/rfq.html", context, 422)
    if notice.startswith("sent:"):
        _, emailed, missing = notice.split(":")
        return _see_other(f"/procurement/rfqs/{rfq_id}?emailed={emailed}&missing={missing}")
    return _see_other(f"/procurement/rfqs/{rfq_id}?notice={notice}")


@router.post(
    "/procurement/rfqs/{rfq_id:uuid}/suppliers/{supplier_id:uuid}/{action}",
    response_class=HTMLResponse,
)
@requires("procurement.rfq.manage")
@screen("procurement.rfq")
def rfq_supplier_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    rfq_id: uuid.UUID,
    supplier_id: uuid.UUID,
    action: str,
) -> Response:
    """remove or decline an invited supplier."""
    try:
        with db.begin_nested():
            if action == "remove":
                rfqs.remove(db, actor, rfq_id, supplier_id)
                notice = "removed"
            elif action == "decline":
                rfqs.decline(db, actor, rfq_id, supplier_id)
                notice = "declined"
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _rfq_context(db, actor, rfq_id, errors=_errors(exc))
        return _render(request, "procurement/rfq.html", context, 422)
    return _see_other(f"/procurement/rfqs/{rfq_id}?notice={notice}")


def _quote_context(
    db: Session, actor: Actor, rfq_id: uuid.UUID, supplier_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    view, entry = rfqs.quote_form(db, actor, rfq_id, supplier_id)
    return {
        "shell": _shell(db, actor, "RFQs"),
        "view": view,
        "entry": entry,
        "vat_codes": [(c, finance.VAT_LABELS[c]) for c in finance.VAT_CODES],
        "vat_rate": finance.vat_rate_on(db, clock.today()),
        "today": clock.today(),
        "values": {},
        "errors": {},
        "page_title": f"Quote from {entry.name}",
        **extra,
    }


@router.get(
    "/procurement/rfqs/{rfq_id:uuid}/quotes/{supplier_id:uuid}", response_class=HTMLResponse
)
@requires("procurement.rfq.manage")
@screen("procurement.quote")
def quote_page(
    request: Request, db: Db, actor: CurrentActor, rfq_id: uuid.UUID, supplier_id: uuid.UUID
) -> HTMLResponse:
    return _render(
        request, "procurement/quote.html", _quote_context(db, actor, rfq_id, supplier_id)
    )


@router.post(
    "/procurement/rfqs/{rfq_id:uuid}/quotes/{supplier_id:uuid}", response_class=HTMLResponse
)
@requires("procurement.rfq.manage")
@screen("procurement.quote")
def save_quote(
    request: Request,
    db: Db,
    actor: CurrentActor,
    rfq_id: uuid.UUID,
    supplier_id: uuid.UUID,
    fields: Fields,
) -> Response:
    data = rfqs.QuoteInput(
        fields.get("reference", ""),
        parse_day(fields.get("received_on", "")),
        parse_day(fields.get("valid_until", "")),
        fields.get("delivery_days", ""),
        fields.get("note", ""),
        {k[6:]: v for k, v in fields.items() if k.startswith("price_")},
        {k[4:]: v for k, v in fields.items() if k.startswith("vat_")},
    )
    try:
        with db.begin_nested():
            rfqs.record_quote(db, actor, rfq_id, supplier_id, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _quote_context(db, actor, rfq_id, supplier_id, values=fields, errors=_errors(exc))
        return _render(request, "procurement/quote.html", context, 422)
    return _see_other(f"/procurement/rfqs/{rfq_id}?notice=quote")


@router.post("/procurement/quotes/{quote_id:uuid}/document", response_class=HTMLResponse)
@requires("procurement.rfq.manage")
@screen("procurement.rfq")
def attach_quote_document(
    db: Db, actor: CurrentActor, quote_id: uuid.UUID, upload: QuoteUpload
) -> Response:
    if upload.errors:
        raise ValidationFailed(field_errors=upload.errors)
    rfq_id = rfqs.rfq_of_quote(db, quote_id)
    rfqs.attach(
        db,
        actor,
        quote_id,
        filename=upload.filename,
        content_type=upload.content_type,
        data=upload.data,
    )
    files.request_scan()
    return _see_other(f"/procurement/rfqs/{rfq_id}?notice=document")


# ------------------------------------------------------------------ supplier performance


def _months(value: str) -> int:
    return int(value) if value in ("3", "6", "12", "24") else 12


@router.get("/procurement/suppliers", response_class=HTMLResponse)
@requires("procurement.rfq.read")
@screen("procurement.suppliers")
def supplier_list(
    request: Request, db: Db, actor: CurrentActor, months: str = "12"
) -> HTMLResponse:
    period = _months(months)
    context = {
        "shell": _shell(db, actor, "Supplier performance"),
        "cards": performance.scorecards(db, actor, months=period),
        "months": period,
        "page_title": "Supplier performance",
    }
    return _render(request, "procurement/suppliers.html", context)


def _supplier_context(
    db: Session, actor: Actor, supplier_id: uuid.UUID, months: int, **extra: Any
) -> dict[str, Any]:
    view = performance.detail(db, actor, supplier_id, months=months)
    return {
        "shell": _shell(db, actor, "Supplier performance"),
        "view": view,
        "months": months,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": view.supplier.name,
        **extra,
    }


@router.get("/procurement/suppliers/{supplier_id:uuid}", response_class=HTMLResponse)
@requires("procurement.rfq.read")
@screen("procurement.supplier")
def supplier_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    supplier_id: uuid.UUID,
    months: str = "12",
    notice: str = "",
) -> HTMLResponse:
    context = _supplier_context(db, actor, supplier_id, _months(months), notice=NOTICES.get(notice))
    return _render(request, "procurement/supplier.html", context)


@router.post(
    "/procurement/suppliers/{supplier_id:uuid}/orders/{order_id:uuid}/rate",
    response_class=HTMLResponse,
)
@requires("procurement.supplier.rate")
@screen("procurement.supplier")
def rate_order(
    request: Request,
    db: Db,
    actor: CurrentActor,
    supplier_id: uuid.UUID,
    order_id: uuid.UUID,
    score: FormStr = "",
    comment: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            performance.rate(db, actor, supplier_id, order_id, score=score, comment=comment)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _supplier_context(
            db,
            actor,
            supplier_id,
            12,
            values={"order": str(order_id), "score": score, "comment": comment},
            errors=_errors(exc),
        )
        return _render(request, "procurement/supplier.html", context, 422)
    return _see_other(f"/procurement/suppliers/{supplier_id}?notice=rated#orders")


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Settings"),
        "settings": setup.settings(db),
        "tiers": setup.tiers(db),
        "level_labels": setup.LEVEL_LABELS,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Procurement settings",
        **extra,
    }


@router.get("/procurement/settings", response_class=HTMLResponse)
@requires("procurement.settings.manage")
@screen("procurement.settings")
def settings_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _settings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "procurement/settings.html", context)


@router.post("/procurement/settings", response_class=HTMLResponse)
@requires("procurement.settings.manage")
@screen("procurement.settings")
def save_settings(request: Request, db: Db, actor: CurrentActor, fields: Fields) -> Response:
    data = setup.SettingsInput(
        fields.get("quote_threshold", ""),
        {k[6:]: v for k, v in fields.items() if k.startswith("above_")},
        {k[7:]: True for k in fields if k.startswith("active_")},
    )
    try:
        with db.begin_nested():
            setup.save(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(db, actor, values=fields, errors=_errors(exc))
        return _render(request, "procurement/settings.html", context, 422)
    return _see_other("/procurement/settings?notice=settings")
