"""Payroll web routes for payroll staff (ADR 0021): runs and their lifecycle, people's
pay, salary structures, rent relief verification and statutory settings. Routes
translate forms and render; the payroll services do the work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll import ledger, reliefs, service, setup, statutory
from app.modules.payroll.statutory import PERCENT_FIELDS
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

RUN_TONES = {
    "draft": "neutral",
    "calculated": "info",
    "reviewed": "info",
    "approved": "success",
    "posted": "success",
    "paid": "success",
}
STEPS = ("draft", "calculated", "reviewed", "approved", "posted", "paid")


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
        db, actor, active_module="payroll", active_section=section, crumbs=["Payroll", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ runs


def _runs_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    today = clock.today()
    return {
        "shell": _shell(db, actor, "Runs"),
        "runs": service.list_runs(db, actor),
        "groups": setup.list_groups(db, active_only=True),
        "can_prepare": bool(engine.check(db, actor.user_id, "payroll.run.prepare")),
        "default_period": today.strftime("%Y-%m"),
        "tones": RUN_TONES,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Payroll runs",
        **extra,
    }


@router.get("/payroll", response_class=HTMLResponse)
@requires("payroll.run.read")
@screen("payroll.runs")
def runs(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _runs_context(db, actor, notice="Run deleted." if notice == "deleted" else None)
    return _render(request, "payroll/runs.html", context)


@router.post("/payroll/runs", response_class=HTMLResponse)
@requires("payroll.run.prepare")
@screen("payroll.runs")
def create_run(
    request: Request,
    db: Db,
    actor: CurrentActor,
    pay_group_id: FormStr = "",
    period: FormStr = "",
) -> Response:
    try:
        year, month = (int(p) for p in period.split("-"))
    except ValueError:
        year, month = 0, 0
    try:
        with db.begin_nested():
            run_id = service.create_run(
                db, actor, pay_group_id=_uuid(pay_group_id), year=year, month=month
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"pay_group_id": pay_group_id, "period": period}
        context = _runs_context(db, actor, values=values, errors=_errors(exc))
        return _render(request, "payroll/runs.html", context, 422)
    return _see_other(f"/payroll/runs/{run_id}")


def _run_context(db: Session, actor: Actor, run_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    run = service.get_run(db, actor, run_id)
    return {
        "shell": _shell(db, actor, "Runs"),
        "run": run,
        "steps": STEPS,
        "step_index": STEPS.index(run.status),
        "tones": RUN_TONES,
        "errors": {},
        "note": "",
        "notice": None,
        "page_title": f"{run.period} · {run.pay_group}",
        "back_href": "/payroll",
        **extra,
    }


@router.get("/payroll/runs/{run_id:uuid}", response_class=HTMLResponse)
@requires("payroll.run.read")
@screen("payroll.run")
def run_page(
    request: Request, db: Db, actor: CurrentActor, run_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    notices = {
        "calculate": "Calculated.",
        "review": "Marked reviewed.",
        "approve": "Approved. Payslips are being generated and scanned.",
        "post": "Posted.",
        "pay": "Marked paid. Employees can now see their payslips.",
        "send_back": "Sent back to draft.",
    }
    context = _run_context(db, actor, run_id, notice=notices.get(notice))
    return _render(request, "payroll/run.html", context)


ACTIONS = {
    "calculate": service.calculate,
    "review": service.review,
    "approve": service.approve,
    "post": service.post,
    "pay": service.mark_paid,
}


@router.post("/payroll/runs/{run_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("payroll.run")
def run_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    run_id: uuid.UUID,
    action: str,
    note: FormStr = "",
) -> Response:
    if action not in {*ACTIONS, "send_back", "delete"}:
        raise NotFound()
    try:
        with db.begin_nested():
            if action == "send_back":
                service.send_back(db, actor, run_id, note=note)
            elif action == "delete":
                service.delete_draft(db, actor, run_id)
            else:
                ACTIONS[action](db, actor, run_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _run_context(db, actor, run_id, errors=_errors(exc), note=note)
        return _render(request, "payroll/run.html", context, 422)
    if action == "delete":
        return _see_other("/payroll?notice=deleted")
    return _see_other(f"/payroll/runs/{run_id}?notice={action}")


# ------------------------------------------------------------------ people's pay


def _people_context(
    db: Session, actor: Actor, *, q: str, selected: str, **extra: Any
) -> dict[str, Any]:
    rows, _ = people.page_in_scope(db, actor, "payroll.pay.read", q=q, size=200)
    summary = setup.current_gross(db, [r.id for r in rows], clock.today())
    chosen = next((r for r in rows if str(r.id) == selected), rows[0] if rows else None)
    can_manage = bool(engine.check(db, actor.user_id, "payroll.pay.manage"))
    return {
        "shell": _shell(db, actor, "People's pay"),
        "rows": rows,
        "summary": summary,
        "q": q,
        "selected": chosen,
        "history": setup.pay_history(db, actor, chosen.id) if chosen else [],
        "deductions": setup.list_deductions(db, actor, chosen.id) if chosen else [],
        "can_manage": can_manage,
        "groups": setup.list_groups(db, active_only=True),
        "structures": setup.list_structures(db),
        "earnings": [c for c in setup.list_components(db, active_only=True) if c.kind == "earning"],
        "deduction_types": [
            c for c in setup.list_components(db, active_only=True) if c.kind == "deduction"
        ],
        "today": clock.today(),
        "values": {},
        "errors": {},
        "editing": "",
        "notice": None,
        "page_title": "People's pay",
        **extra,
    }


@router.get("/payroll/people", response_class=HTMLResponse)
@requires("payroll.pay.read")
@screen("payroll.people")
def people_pay(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _people_context(
        db, actor, q=q, selected=selected, notice="Saved." if notice else None
    )
    return _render(request, "payroll/people.html", context)


@router.post("/payroll/people/{employee_id:uuid}/pay", response_class=HTMLResponse)
@requires("payroll.pay.manage")
@screen("payroll.people")
def set_pay(
    request: Request,
    db: Db,
    actor: CurrentActor,
    employee_id: uuid.UUID,
    pay_group_id: FormStr = "",
    structure_id: FormStr = "",
    start_date: FormStr = "",
    reason: FormStr = "",
    component_id: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    amount: FormList = [],  # noqa: B006
) -> Response:
    amounts = dict(zip(component_id, amount, strict=False))
    try:
        with db.begin_nested():
            setup.set_pay(
                db,
                actor,
                employee_id,
                pay_group_id=_uuid(pay_group_id),
                structure_id=_uuid(structure_id),
                start_date=parse_day(start_date),
                amounts=amounts,
                reason=reason,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "pay_group_id": pay_group_id,
            "structure_id": structure_id,
            "start_date": start_date,
            "reason": reason,
            "amounts": amounts,
        }
        context = _people_context(
            db,
            actor,
            q="",
            selected=str(employee_id),
            values=values,
            errors=_errors(exc),
            editing="pay",
        )
        return _render(request, "payroll/people.html", context, 422)
    return _see_other(f"/payroll/people?selected={employee_id}&notice=1")


@router.post("/payroll/people/{employee_id:uuid}/deductions", response_class=HTMLResponse)
@requires("payroll.pay.manage")
@screen("payroll.people")
def add_deduction(
    request: Request,
    db: Db,
    actor: CurrentActor,
    employee_id: uuid.UUID,
    deduction_component_id: FormStr = "",
    deduction_amount: FormStr = "",
    start_month: FormStr = "",
    end_month: FormStr = "",
    deduction_note: FormStr = "",
) -> Response:
    def month(value: str) -> Any:
        return parse_day(f"{value}-01") if value else None

    try:
        with db.begin_nested():
            setup.add_deduction(
                db,
                actor,
                employee_id,
                component_id=_uuid(deduction_component_id),
                amount=deduction_amount,
                start_month=month(start_month),
                end_month=month(end_month),
                note=deduction_note,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "deduction_component_id": deduction_component_id,
            "deduction_amount": deduction_amount,
            "start_month": start_month,
            "end_month": end_month,
            "deduction_note": deduction_note,
        }
        context = _people_context(
            db,
            actor,
            q="",
            selected=str(employee_id),
            values=values,
            errors=_errors(exc),
            editing="deduction",
        )
        return _render(request, "payroll/people.html", context, 422)
    return _see_other(f"/payroll/people?selected={employee_id}&notice=1#deductions")


@router.post("/payroll/deductions/{deduction_id:uuid}/end", response_class=HTMLResponse)
@requires("payroll.pay.manage")
@screen("payroll.people")
def end_deduction(
    db: Db, actor: CurrentActor, deduction_id: uuid.UUID, employee_id: FormStr = ""
) -> Response:
    setup.end_deduction(db, actor, deduction_id, last_month=clock.today())
    return _see_other(f"/payroll/people?selected={employee_id}&notice=1#deductions")


# ------------------------------------------------------------------ structures


def _structures_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    components = setup.list_components(db)
    return {
        "shell": _shell(db, actor, "Salary structures"),
        "components": components,
        "fixed_earnings": [
            c for c in components if c.kind == "earning" and c.calculation == "fixed"
        ],
        "names": {c.id: c.name for c in components},
        "structures": setup.list_structures(db),
        "groups": setup.list_groups(db),
        "editing": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Salary structures",
        **extra,
    }


@router.get("/payroll/structures", response_class=HTMLResponse)
@requires("payroll.structure.manage")
@screen("payroll.structures")
def structures(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _structures_context(db, actor, notice="Saved." if notice else None)
    return _render(request, "payroll/structures.html", context)


@router.post("/payroll/structures/components", response_class=HTMLResponse)
@requires("payroll.structure.manage")
@screen("payroll.structures")
def save_component(
    request: Request,
    db: Db,
    actor: CurrentActor,
    component_id: FormStr = "",
    code: FormStr = "",
    name: FormStr = "",
    kind: FormStr = "earning",
    calculation: FormStr = "fixed",
    percent: FormStr = "",
    base_component_id: FormStr = "",
    taxable: FormStr = "",
    pensionable: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = setup.ComponentInput(
        code=code,
        name=name,
        kind=kind,
        calculation=calculation,
        percent=percent,
        base_component_id=_uuid(base_component_id),
        taxable=bool(taxable),
        pensionable=bool(pensionable),
        is_active=bool(is_active),
    )
    try:
        with db.begin_nested():
            setup.save_component(db, actor, _uuid(component_id), data)
    except (ValidationFailed, Conflict) as exc:
        context = _structures_context(
            db,
            actor,
            errors=_errors(exc),
            editing=f"component:{component_id or 'new'}",
            values={"code": code, "name": name, "percent": percent},
        )
        return _render(request, "payroll/structures.html", context, 422)
    return _see_other("/payroll/structures?notice=1#components")


@router.post("/payroll/structures/structures", response_class=HTMLResponse)
@requires("payroll.structure.manage")
@screen("payroll.structures")
def save_structure(
    request: Request,
    db: Db,
    actor: CurrentActor,
    structure_id: FormStr = "",
    structure_name: FormStr = "",
    is_active: FormStr = "",
    component_id: FormList = [],  # noqa: B006
    default_amount: FormList = [],  # noqa: B006
    included: FormList = [],  # noqa: B006
) -> Response:
    lines = [
        (cid, amount)
        for cid, amount in zip(component_id, default_amount, strict=False)
        if cid in included
    ]
    try:
        with db.begin_nested():
            setup.save_structure(
                db,
                actor,
                _uuid(structure_id),
                name=structure_name,
                is_active=bool(is_active),
                lines=lines,
            )
    except (ValidationFailed, Conflict) as exc:
        context = _structures_context(
            db, actor, errors=_errors(exc), editing=f"structure:{structure_id or 'new'}"
        )
        return _render(request, "payroll/structures.html", context, 422)
    return _see_other("/payroll/structures?notice=1#structures")


@router.post("/payroll/structures/groups", response_class=HTMLResponse)
@requires("payroll.structure.manage")
@screen("payroll.structures")
def save_group(
    request: Request,
    db: Db,
    actor: CurrentActor,
    group_id: FormStr = "",
    group_name: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            setup.save_group(db, actor, _uuid(group_id), name=group_name, is_active=bool(is_active))
    except (ValidationFailed, Conflict) as exc:
        context = _structures_context(db, actor, errors=_errors(exc), editing="group")
        return _render(request, "payroll/structures.html", context, 422)
    return _see_other("/payroll/structures?notice=1#groups")


# ------------------------------------------------------------------ rent relief


def _reliefs_context(db: Session, actor: Actor, tab: str, **extra: Any) -> dict[str, Any]:
    tab = tab if tab in {"pending", "verified", "rejected"} else "pending"
    return {
        "shell": _shell(db, actor, "Rent relief"),
        "tab": tab,
        "rows": reliefs.queue(db, actor, tab),
        "waiting": len(reliefs.queue(db, actor, "pending")),
        "errors": {},
        "failed": "",
        "note": "",
        "notice": None,
        "page_title": "Rent relief",
        **extra,
    }


@router.get("/payroll/reliefs", response_class=HTMLResponse)
@requires("payroll.relief.verify")
@screen("payroll.reliefs")
def relief_queue(
    request: Request, db: Db, actor: CurrentActor, tab: str = "", notice: str = ""
) -> HTMLResponse:
    notices = {"verify": "Verified. PAYE uses it from the next calculation.", "reject": "Rejected."}
    context = _reliefs_context(db, actor, tab, notice=notices.get(notice))
    return _render(request, "payroll/reliefs.html", context)


@router.post("/payroll/reliefs/{declaration_id:uuid}/{decision}", response_class=HTMLResponse)
@requires("payroll.relief.verify")
@screen("payroll.reliefs")
def decide_relief(
    request: Request,
    db: Db,
    actor: CurrentActor,
    declaration_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
) -> Response:
    if decision not in {"verify", "reject"}:
        raise NotFound()
    try:
        with db.begin_nested():
            reliefs.decide(db, actor, declaration_id, verify=decision == "verify", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _reliefs_context(
            db, actor, "pending", errors=_errors(exc), failed=str(declaration_id), note=note
        )
        return _render(request, "payroll/reliefs.html", context, 422)
    return _see_other(f"/payroll/reliefs?notice={decision}")


# ------------------------------------------------------------------ statutory settings


def _settings_context(
    db: Session, actor: Actor, selected: str = "", **extra: Any
) -> dict[str, Any]:
    sets = statutory.all_sets(db)
    chosen = next((s for s in sets if str(s.id) == selected), sets[0] if sets else None)
    return {
        "shell": _shell(db, actor, "Settings"),
        "sets": sets,
        "selected": chosen,
        "bands": statutory.bands_of(db, chosen) if chosen else [],
        "percent_fields": PERCENT_FIELDS,
        "accounts": ledger.accounts(db),
        "account_fields": ledger.ACCOUNT_FIELDS,
        "banks": ledger.list_banks(db),
        "editing": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Payroll settings",
        **extra,
    }


@router.get("/payroll/settings", response_class=HTMLResponse)
@requires("payroll.settings.manage")
@screen("payroll.settings")
def payroll_settings(
    request: Request, db: Db, actor: CurrentActor, set: str = "", new: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(
        db, actor, set, editing="new" if new else "", notice="Saved." if notice else None
    )
    return _render(request, "payroll/settings.html", context)


@router.post("/payroll/settings", response_class=HTMLResponse)
@requires("payroll.settings.manage")
@screen("payroll.settings")
def save_settings(
    request: Request,
    db: Db,
    actor: CurrentActor,
    set_id: FormStr = "",
    effective_from: FormStr = "",
    label: FormStr = "",
    rent_relief_rate: FormStr = "",
    rent_relief_cap: FormStr = "",
    pension_employee_rate: FormStr = "",
    pension_employer_rate: FormStr = "",
    nhf_rate: FormStr = "",
    nhis_rate: FormStr = "",
    nsitf_rate: FormStr = "",
    itf_rate: FormStr = "",
    minimum_wage: FormStr = "",
    overtime_multiplier: FormStr = "",
    hours_per_month: FormStr = "",
    band_width: FormList = [],  # noqa: B006
    band_rate: FormList = [],  # noqa: B006
) -> Response:
    rates = {
        "rent_relief_rate": rent_relief_rate,
        "rent_relief_cap": rent_relief_cap,
        "pension_employee_rate": pension_employee_rate,
        "pension_employer_rate": pension_employer_rate,
        "nhf_rate": nhf_rate,
        "nhis_rate": nhis_rate,
        "nsitf_rate": nsitf_rate,
        "itf_rate": itf_rate,
        "minimum_wage": minimum_wage,
        "overtime_multiplier": overtime_multiplier,
        "hours_per_month": hours_per_month,
    }
    bands = list(zip(band_width, band_rate, strict=False))
    try:
        with db.begin_nested():
            saved = statutory.save_set(
                db,
                actor,
                _uuid(set_id),
                effective_from=parse_day(effective_from),
                label=label,
                rates=rates,
                bands=bands,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {**rates, "effective_from": effective_from, "label": label, "bands": bands}
        context = _settings_context(
            db,
            actor,
            set_id,
            values=values,
            errors=_errors(exc),
            editing="new" if not set_id else "set",
        )
        return _render(request, "payroll/settings.html", context, 422)
    return _see_other(f"/payroll/settings?set={saved}&notice=1")


@router.post("/payroll/settings/accounts", response_class=HTMLResponse)
@requires("payroll.settings.manage")
@screen("payroll.settings")
def save_accounts(
    request: Request,
    db: Db,
    actor: CurrentActor,
    salaries: FormStr = "",
    employer_pension: FormStr = "",
    employer_nsitf: FormStr = "",
    paye_payable: FormStr = "",
    pension_payable: FormStr = "",
    nhf_payable: FormStr = "",
    nhis_payable: FormStr = "",
    nsitf_payable: FormStr = "",
    deductions_payable: FormStr = "",
    net_pay_payable: FormStr = "",
    staff_loans: FormStr = "",
    bank: FormStr = "",
) -> Response:
    values = {
        "salaries": salaries,
        "employer_pension": employer_pension,
        "employer_nsitf": employer_nsitf,
        "paye_payable": paye_payable,
        "pension_payable": pension_payable,
        "nhf_payable": nhf_payable,
        "nhis_payable": nhis_payable,
        "nsitf_payable": nsitf_payable,
        "deductions_payable": deductions_payable,
        "net_pay_payable": net_pay_payable,
        "staff_loans": staff_loans,
        "bank": bank,
    }
    try:
        with db.begin_nested():
            ledger.save_accounts(db, actor, values)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(
            db, actor, values=values, errors=_errors(exc), editing="accounts"
        )
        return _render(request, "payroll/settings.html", context, 422)
    return _see_other("/payroll/settings?notice=1#accounts")


@router.post("/payroll/settings/banks", response_class=HTMLResponse)
@requires("payroll.settings.manage")
@screen("payroll.settings")
def save_bank(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bank_id: FormStr = "",
    bank_name: FormStr = "",
    bank_code: FormStr = "",
    other_names: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            ledger.save_bank(
                db,
                actor,
                _uuid(bank_id),
                name=bank_name,
                code=bank_code,
                other_names=other_names,
                is_active=bool(is_active) or not bank_id,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"bank_name": bank_name, "bank_code": bank_code, "other_names": other_names}
        context = _settings_context(
            db, actor, values=values, errors=_errors(exc), editing=f"bank:{bank_id or 'new'}"
        )
        return _render(request, "payroll/settings.html", context, 422)
    return _see_other("/payroll/settings?notice=1#banks")
