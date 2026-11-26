"""Fixed asset web routes (ADR 0027): the register, one asset with its revaluations and
disposals, categories and settings, and depreciation runs. Routes translate forms and
render; the services do the work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, assets, banking, depreciation, periods
from app.modules.finance.assets import AssetInput, CategoryInput, SettingsInput
from app.modules.finance.banking import can, uuid_or_none
from app.modules.finance.depreciation import APPROVE, MANAGE
from app.modules.finance.models import DEPRECIATION_METHODS, AssetCategory, FixedAsset
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
SECTION = "Fixed assets"
ASSET_FIELDS = (
    "name",
    "category_id",
    "acquired_on",
    "cost",
    "residual",
    "life_months",
    "rate",
    "cost_centre_id",
    "serial",
    "location",
    "description",
    "opening_accumulated",
    "opening_to",
    "bill_line_id",
)
NOTICES = {
    "registered": "Registered.",
    "saved": "Saved.",
    "deleted": "Deleted.",
    "recorded": "Recorded. Someone other than you approves it.",
    "approved": "Approved and posted.",
    "removed": "The waiting change was deleted.",
    "prepared": "Prepared. Someone other than you posts it.",
    "posted": "Posted.",
    "nothing": "No month is ready to run yet.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor) -> Any:
    return build_shell(
        db, actor, active_module="finance", active_section=SECTION, crumbs=["Finance", SECTION]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _threshold_note(db: Session) -> str:
    threshold = periods.settings(db).capitalisation_threshold
    return (
        f"Registered. Its cost is below the capitalisation threshold of ₦{threshold:,.2f}, "
        "so check it belongs in the register."
    )


# ------------------------------------------------------------------ the register


@router.get("/finance/assets", response_class=HTMLResponse)
@requires("finance.asset.read")
@screen("finance.assets")
def asset_register(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category: str = "",
    status: str = "active",
    q: str = "",
    notice: str = "",
) -> HTMLResponse:
    status = status if status in ("active", "disposed", "all") else "active"
    context = {
        "shell": _shell(db, actor),
        "rows": assets.register_rows(db, actor, category=category, status=status, q=q),
        "totals": assets.totals(db),
        "candidates": assets.from_bills(db, actor),
        "checks": assets.ledger_check(db, actor),
        "categories": assets.category_choices(db),
        "category": category,
        "status": status,
        "q": q,
        "labels": assets.STATUS_LABELS,
        "tones": assets.STATUS_TONES,
        "can_manage": can(db, actor, MANAGE),
        "notice": NOTICES.get(notice),
        "page_title": SECTION,
    }
    return _render(request, "finance/assets.html", context)


def _form_context(
    db: Session, actor: Actor, *, asset: FixedAsset | None, **extra: Any
) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor),
        "mode": "edit" if asset else "new",
        "asset": asset,
        "categories": assets.category_choices(db),
        "cost_centres": org.cost_centre_choices(db),
        "methods": assets.METHOD_LABELS,
        "figures_locked": False,
        "candidate": None,
        "values": {},
        "errors": {},
        "page_title": f"Edit {asset.number}" if asset else "Register an asset",
        **extra,
    }


@router.get("/finance/assets/new", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_form")
def new_asset(request: Request, db: Db, actor: CurrentActor, bill_line: str = "") -> HTMLResponse:
    candidate = None
    values: dict[str, str] = {}
    line_id = uuid_or_none(bill_line)
    if line_id:
        candidate = assets.bill_candidate(db, actor, line_id)
    if candidate:
        category = db.get(AssetCategory, candidate.category_id)
        values = {
            "name": candidate.description,
            "category_id": str(candidate.category_id),
            "acquired_on": candidate.bill_date.isoformat(),
            "cost": f"{candidate.net:.2f}",
            "life_months": str(category.life_months or "") if category else "",
            "rate": f"{category.rate:f}" if category and category.rate is not None else "",
            "bill_line_id": str(candidate.line_id),
        }
    context = _form_context(db, actor, asset=None, candidate=candidate, values=values)
    return _render(request, "finance/asset_form.html", context)


def _month(value: str) -> str:
    """An `<input type="month">` value (2026-03) as its first day."""
    return f"{value}-01" if len(value) == 7 else value


def _asset_input(form: dict[str, str]) -> AssetInput:
    return AssetInput(
        name=form["name"],
        category_id=form["category_id"],
        acquired_on=parse_day(form["acquired_on"]),
        cost=form["cost"],
        residual=form["residual"],
        life_months=form["life_months"],
        rate=form["rate"],
        cost_centre_id=form["cost_centre_id"],
        serial=form["serial"],
        location=form["location"],
        description=form["description"],
        opening_accumulated=form["opening_accumulated"],
        opening_to=parse_day(_month(form["opening_to"])),
        bill_line_id=form["bill_line_id"],
    )


def _asset_form(
    name: FormStr = "",
    category_id: FormStr = "",
    acquired_on: FormStr = "",
    cost: FormStr = "",
    residual: FormStr = "",
    life_months: FormStr = "",
    rate: FormStr = "",
    cost_centre_id: FormStr = "",
    serial: FormStr = "",
    location: FormStr = "",
    description: FormStr = "",
    opening_accumulated: FormStr = "",
    opening_to: FormStr = "",
    bill_line_id: FormStr = "",
) -> dict[str, str]:
    return dict(
        zip(
            ASSET_FIELDS,
            (
                name,
                category_id,
                acquired_on,
                cost,
                residual,
                life_months,
                rate,
                cost_centre_id,
                serial,
                location,
                description,
                opening_accumulated,
                opening_to,
                bill_line_id,
            ),
            strict=True,
        )
    )


AssetForm = Annotated[dict[str, str], Depends(_asset_form)]


@router.post("/finance/assets/new", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_form")
def register_asset(request: Request, db: Db, actor: CurrentActor, form: AssetForm) -> Response:
    try:
        with db.begin_nested():
            asset_id, warning = assets.register(db, actor, None, _asset_input(form))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        line_id = uuid_or_none(form["bill_line_id"])
        candidate = assets.bill_candidate(db, actor, line_id) if line_id else None
        context = _form_context(
            db, actor, asset=None, candidate=candidate, values=form, errors=_errors(exc)
        )
        return _render(request, "finance/asset_form.html", context, 422)
    return _see_other(f"/finance/assets/{asset_id}?notice={'below' if warning else 'registered'}")


@router.get("/finance/assets/{asset_id:uuid}/edit", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_form")
def edit_asset(request: Request, db: Db, actor: CurrentActor, asset_id: uuid.UUID) -> HTMLResponse:
    view = assets.view(db, actor, asset_id)
    a = view.asset
    values = {
        "name": a.name,
        "category_id": str(a.category_id),
        "acquired_on": a.acquired_on.isoformat(),
        "cost": f"{a.cost:.2f}",
        "residual": f"{a.residual:.2f}",
        "life_months": str(a.life_months or ""),
        "rate": f"{a.rate:f}" if a.rate is not None else "",
        "cost_centre_id": str(a.cost_centre_id or ""),
        "serial": a.serial or "",
        "location": a.location or "",
        "description": a.description or "",
        "opening_accumulated": f"{a.accumulated:.2f}" if a.accumulated else "",
        "opening_to": f"{a.depreciated_to:%Y-%m}" if a.depreciated_to else "",
    }
    context = _form_context(db, actor, asset=a, values=values, figures_locked=view.figures_locked)
    return _render(request, "finance/asset_form.html", context)


@router.post("/finance/assets/{asset_id:uuid}/edit", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_form")
def save_asset(
    request: Request, db: Db, actor: CurrentActor, asset_id: uuid.UUID, form: AssetForm
) -> Response:
    try:
        with db.begin_nested():
            assets.register(db, actor, asset_id, _asset_input(form))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        view = assets.view(db, actor, asset_id)
        context = _form_context(
            db,
            actor,
            asset=view.asset,
            values=form,
            errors=_errors(exc),
            figures_locked=view.figures_locked,
        )
        return _render(request, "finance/asset_form.html", context, 422)
    return _see_other(f"/finance/assets/{asset_id}?notice=saved")


def _asset_context(db: Session, actor: Actor, asset_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    view = assets.view(db, actor, asset_id)
    return {
        "shell": _shell(db, actor),
        "v": view,
        "a": view.asset,
        "banks": banking.choices(db),
        "labels": assets.STATUS_LABELS,
        "tones": assets.STATUS_TONES,
        "change_labels": assets.CHANGE_LABELS,
        "today": clock.today(),
        "form": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{view.asset.number} {view.asset.name}",
        **extra,
    }


@router.get("/finance/assets/{asset_id:uuid}", response_class=HTMLResponse)
@requires("finance.asset.read")
@screen("finance.asset")
def asset_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    asset_id: uuid.UUID,
    notice: str = "",
    record: str = "",
) -> HTMLResponse:
    message = _threshold_note(db) if notice == "below" else NOTICES.get(notice)
    form = record if record in assets.CHANGE_LABELS else ""
    context = _asset_context(db, actor, asset_id, notice=message, form=form)
    return _render(request, "finance/asset.html", context)


@router.post("/finance/assets/{asset_id:uuid}/delete", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset")
def delete_asset(request: Request, db: Db, actor: CurrentActor, asset_id: uuid.UUID) -> Response:
    try:
        with db.begin_nested():
            assets.delete_asset(db, actor, asset_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _asset_context(db, actor, asset_id, errors=_errors(exc))
        return _render(request, "finance/asset.html", context, 422)
    return _see_other("/finance/assets?notice=deleted")


@router.post("/finance/assets/{asset_id:uuid}/change", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset")
def record_change(
    request: Request,
    db: Db,
    actor: CurrentActor,
    asset_id: uuid.UUID,
    kind: FormStr = "",
    on: FormStr = "",
    amount: FormStr = "",
    bank_id: FormStr = "",
    reason: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            assets.record_change(
                db,
                actor,
                asset_id,
                kind=kind,
                on=parse_day(on),
                amount=amount,
                bank_id=bank_id,
                reason=reason,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"on": on, "amount": amount, "bank_id": bank_id, "reason": reason}
        context = _asset_context(db, actor, asset_id, form=kind, values=values, errors=_errors(exc))
        return _render(request, "finance/asset.html", context, 422)
    return _see_other(f"/finance/assets/{asset_id}?notice=recorded")


@router.post("/finance/asset-changes/{change_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.asset.read")
@screen("finance.asset")
def change_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    change_id: uuid.UUID,
    action: str,
    asset_id: FormStr = "",
) -> Response:
    """approve or delete: each service checks its own permission."""
    owner = uuid_or_none(asset_id)
    if owner is None:
        return _see_other("/finance/assets")
    try:
        with db.begin_nested():
            if action == "approve":
                assets.approve_change(db, actor, change_id)
            elif action == "delete":
                assets.delete_change(db, actor, change_id)
            else:
                raise Conflict("Choose what to do with the change.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _asset_context(db, actor, owner, errors=_errors(exc))
        return _render(request, "finance/asset.html", context, 422)
    notice = "approved" if action == "approve" else "removed"
    return _see_other(f"/finance/assets/{owner}?notice={notice}")


# ------------------------------------------------------------------ categories and settings


def _categories_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    settings = periods.settings(db)
    return {
        "shell": _shell(db, actor),
        "rows": assets.categories(db, actor),
        "postable": accounts.postable(db),
        "methods": [(m, assets.METHOD_LABELS[m]) for m in DEPRECIATION_METHODS],
        "method_labels": assets.METHOD_LABELS,
        "settings": settings,
        "can_manage": can(db, actor, MANAGE),
        "editing": "",
        "category": None,
        "form": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Asset categories",
        **extra,
    }


@router.get("/finance/assets/categories", response_class=HTMLResponse)
@requires("finance.asset.read")
@screen("finance.asset_categories")
def asset_categories(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    category = db.get(AssetCategory, uuid_or_none(edit)) if uuid_or_none(edit) else None
    context = _categories_context(
        db,
        actor,
        editing="new" if edit == "new" else (str(category.id) if category else ""),
        category=category,
        notice=NOTICES.get(notice),
    )
    return _render(request, "finance/asset_categories.html", context)


@router.post("/finance/assets/categories", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_categories")
def save_category(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category_id: FormStr = "",
    name: FormStr = "",
    method: FormStr = "straight_line",
    life_months: FormStr = "",
    rate: FormStr = "",
    cost_account_id: FormStr = "",
    accumulated_account_id: FormStr = "",
    expense_account_id: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    existing = uuid_or_none(category_id)
    data = CategoryInput(
        name,
        method,
        life_months,
        rate,
        cost_account_id,
        accumulated_account_id,
        expense_account_id,
        bool(is_active) or not existing,
    )
    try:
        with db.begin_nested():
            assets.save_category(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "name": name,
            "method": method,
            "life_months": life_months,
            "rate": rate,
            "cost_account_id": cost_account_id,
            "accumulated_account_id": accumulated_account_id,
            "expense_account_id": expense_account_id,
        }
        context = _categories_context(
            db,
            actor,
            editing=category_id or "new",
            category=db.get(AssetCategory, existing) if existing else None,
            form="category",
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/asset_categories.html", context, 422)
    return _see_other("/finance/assets/categories?notice=saved")


@router.post("/finance/assets/settings", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.asset_categories")
def save_settings(
    request: Request,
    db: Db,
    actor: CurrentActor,
    threshold: FormStr = "",
    reserve_code: FormStr = "",
    disposal_code: FormStr = "",
    loss_code: FormStr = "",
) -> Response:
    data = SettingsInput(threshold, reserve_code, disposal_code, loss_code)
    try:
        with db.begin_nested():
            assets.save_settings(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "threshold": threshold,
            "reserve_code": reserve_code,
            "disposal_code": disposal_code,
            "loss_code": loss_code,
        }
        context = _categories_context(
            db, actor, form="settings", values=values, errors=_errors(exc)
        )
        return _render(request, "finance/asset_categories.html", context, 422)
    return _see_other("/finance/assets/categories?notice=saved")


# ------------------------------------------------------------------ depreciation


def _depreciation_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor),
        "run": depreciation.current(db, actor),
        "posted": depreciation.posted_runs(db, actor),
        "next_month": depreciation.next_month(db),
        "this_month": depreciation.first_of(clock.today()),
        "can_manage": can(db, actor, MANAGE),
        "can_approve": can(db, actor, APPROVE),
        "errors": {},
        "notice": None,
        "page_title": "Depreciation",
        **extra,
    }


@router.get("/finance/depreciation", response_class=HTMLResponse)
@requires("finance.asset.read")
@screen("finance.depreciation")
def depreciation_runs(
    request: Request, db: Db, actor: CurrentActor, notice: str = ""
) -> HTMLResponse:
    context = _depreciation_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "finance/depreciation.html", context)


@router.post("/finance/depreciation/prepare", response_class=HTMLResponse)
@requires("finance.asset.manage")
@screen("finance.depreciation")
def prepare_run(request: Request, db: Db, actor: CurrentActor) -> Response:
    try:
        with db.begin_nested():
            run = depreciation.prepare(db, actor)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _depreciation_context(db, actor, errors=_errors(exc))
        return _render(request, "finance/depreciation.html", context, 422)
    return _see_other(f"/finance/depreciation?notice={'prepared' if run else 'nothing'}")


@router.post("/finance/depreciation/{run_id:uuid}/post", response_class=HTMLResponse)
@requires("finance.asset.approve")
@screen("finance.depreciation")
def post_run(request: Request, db: Db, actor: CurrentActor, run_id: uuid.UUID) -> Response:
    try:
        with db.begin_nested():
            depreciation.post(db, actor, run_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _depreciation_context(db, actor, errors=_errors(exc))
        return _render(request, "finance/depreciation.html", context, 422)
    return _see_other("/finance/depreciation?notice=posted")
