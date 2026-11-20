"""Payroll web routes for what comes out of a run (ADR 0022): remittances and their CSV
files, staff loans and salary advances, and year-end tax certificates. Routes translate
forms and render; the payroll services do the work."""

import uuid
from datetime import date
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
from app.modules.payroll import certificates, loans, remittances
from app.web.access import require_actor
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

LOAN_TONES = {
    "pending": "warning",
    "repaying": "info",
    "cleared": "success",
    "rejected": "danger",
    "cancelled": "neutral",
}
NO_STORE = {"Cache-Control": "no-store"}


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


def _download(filename: str, body: str | bytes, media_type: str) -> Response:
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', **NO_STORE},
    )


# ------------------------------------------------------------------ remittances


@router.get("/payroll/remittances", response_class=HTMLResponse)
@requires("payroll.run.read")
@screen("payroll.remittances")
def remittances_page(request: Request, db: Db, actor: CurrentActor, run: str = "") -> HTMLResponse:
    ready = remittances.ready_runs(db)
    chosen = _uuid(run) or (ready[0][0] if ready else None)
    view = None
    if chosen is not None:
        try:
            view = remittances.summary(db, actor, chosen)
        except (NotFound, Conflict):
            view = None
    context = {
        "shell": _shell(db, actor, "Remittances"),
        "ready": ready,
        "view": view,
        "page_title": "Remittances",
    }
    return _render(request, "payroll/remittances.html", context)


@router.get("/payroll/remittances/{run_id:uuid}/bank.csv")
@requires("payroll.remittance.export")
def bank_csv(db: Db, actor: CurrentActor, run_id: uuid.UUID) -> Response:
    filename, text = remittances.bank_file(db, actor, run_id)
    return _download(filename, text, "text/csv; charset=utf-8")


@router.get("/payroll/remittances/{run_id:uuid}/paye.csv")
@requires("payroll.remittance.export")
def paye_csv(db: Db, actor: CurrentActor, run_id: uuid.UUID, state: str = "") -> Response:
    filename, text = remittances.paye_schedule(db, actor, run_id, state)
    return _download(filename, text, "text/csv; charset=utf-8")


@router.get("/payroll/remittances/{run_id:uuid}/pension.csv")
@requires("payroll.remittance.export")
def pension_csv(db: Db, actor: CurrentActor, run_id: uuid.UUID, pfa: str = "") -> Response:
    filename, text = remittances.pension_schedule(db, actor, run_id, pfa)
    return _download(filename, text, "text/csv; charset=utf-8")


@router.get("/payroll/remittances/{run_id:uuid}/nhf.csv")
@requires("payroll.remittance.export")
def nhf_csv(db: Db, actor: CurrentActor, run_id: uuid.UUID) -> Response:
    filename, text = remittances.nhf_schedule(db, actor, run_id)
    return _download(filename, text, "text/csv; charset=utf-8")


# ------------------------------------------------------------------ loans


def _loans_context(db: Session, actor: Actor, tab: str, **extra: Any) -> dict[str, Any]:
    tab = tab if tab in loans.TABS else "pending"
    can_manage = bool(engine.check(db, actor.user_id, loans.MANAGE))
    return {
        "shell": _shell(db, actor, "Loans"),
        "tab": tab,
        "rows": loans.list_loans(db, actor, tab),
        "waiting": loans.counts(db, actor)["pending"],
        "can_manage": can_manage,
        "people": people.assignable_people(db) if can_manage else [],
        "this_month": clock.today().strftime("%Y-%m"),
        "tones": LOAN_TONES,
        "values": {},
        "errors": {},
        "failed": "",
        "notice": None,
        "page_title": "Loans and salary advances",
        **extra,
    }


@router.get("/payroll/loans", response_class=HTMLResponse)
@requires("payroll.loan.read")
@screen("payroll.loans")
def loans_page(
    request: Request, db: Db, actor: CurrentActor, tab: str = "", notice: str = ""
) -> HTMLResponse:
    notices = {
        "recorded": "Recorded. A Finance Manager approves it before payroll deducts it.",
        "approve": "Approved. Deductions start in the first month's run.",
        "reject": "Rejected.",
        "withdraw": "Withdrawn.",
    }
    context = _loans_context(db, actor, tab, notice=notices.get(notice))
    return _render(request, "payroll/loans.html", context)


def _month(value: str) -> date | None:
    try:
        year, month = (int(p) for p in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return None


@router.post("/payroll/loans", response_class=HTMLResponse)
@requires("payroll.loan.manage")
@screen("payroll.loans")
def record_loan(
    request: Request,
    db: Db,
    actor: CurrentActor,
    employee_id: FormStr = "",
    kind: FormStr = "loan",
    amount: FormStr = "",
    instalment: FormStr = "",
    first_month: FormStr = "",
    note: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            loans.record(
                db,
                actor,
                employee_id=_uuid(employee_id),
                kind=kind,
                amount=amount,
                instalment=instalment,
                first_month=_month(first_month),
                note=note,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "employee_id": employee_id,
            "kind": kind,
            "amount": amount,
            "instalment": instalment,
            "first_month": first_month,
            "note": note,
        }
        context = _loans_context(db, actor, "pending", values=values, errors=_errors(exc))
        return _render(request, "payroll/loans.html", context, 422)
    return RedirectResponse("/payroll/loans?notice=recorded", status_code=303)


@router.post("/payroll/loans/{loan_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("payroll.loans")
def loan_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    loan_id: uuid.UUID,
    action: str,
    note: FormStr = "",
) -> Response:
    if action not in {"approve", "reject", "withdraw"}:
        raise NotFound()
    try:
        with db.begin_nested():
            if action == "withdraw":
                loans.withdraw(db, actor, loan_id)
            else:
                loans.decide(db, actor, loan_id, approve=action == "approve", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _loans_context(
            db, actor, "pending", errors=_errors(exc), failed=str(loan_id), note=note
        )
        return _render(request, "payroll/loans.html", context, 422)
    tab = "repaying" if action == "approve" else "pending"
    return RedirectResponse(f"/payroll/loans?tab={tab}&notice={action}", status_code=303)


# ------------------------------------------------------------------ tax certificates


@router.get("/payroll/certificates", response_class=HTMLResponse)
@requires("payroll.certificate.read")
@screen("payroll.certificates")
def certificates_page(
    request: Request, db: Db, actor: CurrentActor, year: int | None = None
) -> HTMLResponse:
    view = certificates.year_view(db, actor, year)
    totals = {
        key: sum((getattr(r, key) for r in view.rows), start=0)
        for key in ("gross", "pension", "nhf", "rent_relief", "taxable", "paye")
    }
    context = {
        "shell": _shell(db, actor, "Tax certificates"),
        "view": view,
        "totals": totals,
        "page_title": "Tax certificates",
    }
    return _render(request, "payroll/certificates.html", context)


@router.get("/payroll/certificates/{year:int}/{employee_id:uuid}/pdf")
@requires("payroll.certificate.read")
def certificate_pdf(db: Db, actor: CurrentActor, year: int, employee_id: uuid.UUID) -> Response:
    filename, body = certificates.certificate(db, actor, employee_id, year)
    return _download(filename, body, "application/pdf")
