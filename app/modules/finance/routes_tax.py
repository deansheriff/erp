"""Tax web routes (ADR 0024): VAT rates, withholding-tax and stamp duty types, the
receivables settings, and monthly tax returns."""

import uuid
from dataclasses import asdict
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
from app.modules.finance import accounts, banking, periods, receipts, tax
from app.modules.finance.models import TAXES
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

RETURN_TONES = {"draft": "neutral", "filed": "info", "paid": "success", "overdue": "danger"}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="finance", active_section=section, crumbs=["Finance", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Tax settings"),
        "vat_rates": tax.vat_rates(db),
        "current_rate": tax.vat_rate_on(db, clock.today()),
        "wht_types": tax.wht_types(db),
        "stamp_types": tax.stamp_types(db),
        "basis_labels": tax.BASIS_LABELS,
        "settings": periods.settings(db),
        "setting_accounts": tax.SETTING_ACCOUNTS,
        "postable": accounts.postable(db),
        "can_manage": bool(engine.check(db, actor.user_id, tax.MANAGE)),
        "editing_stamp": "",
        "form": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Tax settings",
        **extra,
    }


@router.get("/finance/tax", response_class=HTMLResponse)
@requires("finance.tax.read")
@screen("finance.tax")
def tax_settings(
    request: Request, db: Db, actor: CurrentActor, stamp: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(db, actor, editing_stamp=stamp, notice="Saved." if notice else None)
    return _render(request, "finance/tax.html", context)


def _settings_action(
    request: Request, db: Session, actor: Actor, form: str, values: dict[str, Any], run: Any
) -> Response:
    try:
        with db.begin_nested():
            run()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(
            db,
            actor,
            form=form,
            values=values,
            errors=_errors(exc),
            editing_stamp=values.get("stamp_id", ""),
        )
        return _render(request, "finance/tax.html", context, 422)
    return _see_other(f"/finance/tax?notice=1#{form}")


@router.post("/finance/tax/vat", response_class=HTMLResponse)
@requires("finance.tax.manage")
@screen("finance.tax")
def add_vat_rate(
    request: Request,
    db: Db,
    actor: CurrentActor,
    effective_from: FormStr = "",
    rate: FormStr = "",
) -> Response:
    return _settings_action(
        request,
        db,
        actor,
        "vat",
        {"effective_from": effective_from, "rate": rate},
        lambda: tax.add_vat_rate(db, actor, effective_from=parse_day(effective_from), rate=rate),
    )


@router.post("/finance/tax/wht", response_class=HTMLResponse)
@requires("finance.tax.manage")
@screen("finance.tax")
def save_wht(
    request: Request,
    db: Db,
    actor: CurrentActor,
    wht_type_id: FormList = [],  # noqa: B006  (FastAPI form lists)
    company_rate: FormList = [],  # noqa: B006
    individual_rate: FormList = [],  # noqa: B006
    active: FormList = [],  # noqa: B006
) -> Response:
    rows = [
        tax.WhtRateInput(
            wid,
            company_rate[i] if i < len(company_rate) else "",
            individual_rate[i] if i < len(individual_rate) else "",
            wid in active,
        )
        for i, wid in enumerate(wht_type_id)
    ]
    values = {
        f"{kind}_{r.wht_type_id}": getattr(r, f"{kind}_rate")
        for r in rows
        for kind in ("company", "individual")
    }
    return _settings_action(
        request, db, actor, "wht", values, lambda: tax.save_wht_rates(db, actor, rows)
    )


@router.post("/finance/tax/stamp", response_class=HTMLResponse)
@requires("finance.tax.manage")
@screen("finance.tax")
def save_stamp(
    request: Request,
    db: Db,
    actor: CurrentActor,
    stamp_id: FormStr = "",
    stamp_name: FormStr = "",
    basis: FormStr = "",
    value: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    values = {
        "stamp_id": stamp_id or "new",
        "stamp_name": stamp_name,
        "basis": basis,
        "value": value,
        "is_active": is_active,
    }
    return _settings_action(
        request,
        db,
        actor,
        "stamp",
        values,
        lambda: tax.save_stamp_type(
            db,
            actor,
            _uuid(stamp_id),
            name=stamp_name,
            basis=basis,
            value=value,
            is_active=bool(is_active) or not stamp_id,
        ),
    )


@router.post("/finance/tax/settings", response_class=HTMLResponse)
@requires("finance.tax.manage")
@screen("finance.tax")
def save_receivables_settings(
    request: Request,
    db: Db,
    actor: CurrentActor,
    receivables_code: FormStr = "",
    vat_output_code: FormStr = "",
    vat_input_code: FormStr = "",
    wht_receivable_code: FormStr = "",
    wht_payable_code: FormStr = "",
    stamp_duty_code: FormStr = "",
    default_terms_days: FormStr = "",
    remind_before_days: FormStr = "",
    remind_on_due: FormStr = "",
    remind_every_days: FormStr = "",
    return_due_day: FormStr = "",
    payables_code: FormStr = "",
    price_tolerance: FormStr = "",
) -> Response:
    data = tax.SettingsInput(
        receivables_code,
        vat_output_code,
        vat_input_code,
        wht_receivable_code,
        wht_payable_code,
        stamp_duty_code,
        default_terms_days,
        remind_before_days,
        bool(remind_on_due),
        remind_every_days,
        return_due_day,
        payables_code,
        price_tolerance,
    )
    values = {**asdict(data), "remind_on_due": remind_on_due}
    return _settings_action(
        request, db, actor, "settings", values, lambda: tax.save_settings(db, actor, data)
    )


# ------------------------------------------------------------------ returns


def _month(value: str) -> date | None:
    try:
        year, month = (int(p) for p in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return None


def _returns_context(
    db: Session, actor: Actor, tax_key: str, period: date | None, **extra: Any
) -> dict[str, Any]:
    today = clock.today()
    rows = tax.returns(db, actor, tax_key, today=today)
    if period is None and rows:
        period = rows[0].period_start
    detail = tax.return_detail(db, actor, tax_key, period, today=today) if period else None
    return {
        "shell": _shell(db, actor, "Tax returns"),
        "tax": tax_key,
        "taxes": [(t, tax.TAX_LABELS[t]) for t in TAXES],
        "tax_label": tax.TAX_LABELS[tax_key],
        "rows": rows,
        "detail": detail,
        "tones": RETURN_TONES,
        "awaiting": receipts.list_receipts(
            db, actor, customer_id=None, start=None, end=None, awaiting=True
        )
        if tax_key == "wht"
        else [],
        "can_manage": bool(engine.check(db, actor.user_id, tax.MANAGE)),
        "banks": banking.choices(db),
        "today": today,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Tax returns",
        **extra,
    }


@router.get("/finance/tax/returns", response_class=HTMLResponse)
@requires("finance.tax.read")
@screen("finance.tax_returns")
def tax_returns(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tax: str = "vat",
    period: str = "",
    notice: str = "",
) -> HTMLResponse:
    key = tax if tax in TAXES else "vat"
    notices = {"filed": "Marked as filed.", "paid": "Marked as paid."}
    context = _returns_context(db, actor, key, _month(period), notice=notices.get(notice))
    return _render(request, "finance/tax_returns.html", context)


@router.post("/finance/tax/returns/{tax_key}/{period}/{action}", response_class=HTMLResponse)
@requires("finance.tax.manage")
@screen("finance.tax_returns")
def update_return(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tax_key: str,
    period: str,
    action: str,
    on: FormStr = "",
    reference: FormStr = "",
    paid_from: FormStr = "",
) -> Response:
    start = _month(period)
    key = tax_key if tax_key in TAXES else "vat"
    try:
        with db.begin_nested():
            if start is None or action not in {"file", "paid"}:
                raise Conflict("Choose a return.")
            if action == "file":
                tax.file_return(
                    db,
                    actor,
                    key,
                    start,
                    filed_on=parse_day(on),
                    reference=reference,
                    today=clock.today(),
                )
            else:
                tax.mark_paid(
                    db,
                    actor,
                    key,
                    start,
                    paid_on=parse_day(on),
                    reference=reference,
                    bank_account_id=paid_from,
                )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        # The forms share two inputs; map the service's keys onto them.
        errors = {
            ("on" if k.endswith("_on") else "reference" if k.endswith("_reference") else k): v
            for k, v in errors.items()
        }
        context = _returns_context(
            db,
            actor,
            key,
            start,
            values={"on": on, "reference": reference, "action": action, "paid_from": paid_from},
            errors=errors,
        )
        return _render(request, "finance/tax_returns.html", context, 422)
    notice = "filed" if action == "file" else "paid"
    return _see_other(f"/finance/tax/returns?tax={key}&period={period}&notice={notice}")
