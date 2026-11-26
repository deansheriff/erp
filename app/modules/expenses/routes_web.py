"""Expense web routes (ADR 0020): my expenses, the claim and advance pages, approvals,
payments and settings. Routes translate forms and render; the expense services do the
work."""

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.expenses import service, settings
from app.modules.expenses.files import EXPENSE_RECEIPT
from app.modules.finance import service as finance
from app.modules.hr import service as people
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
ReceiptUpload = Annotated[ReceivedUpload, Depends(upload_body(EXPENSE_RECEIPT.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

STATUS = {
    "draft": ("neutral", "Draft"),
    "submitted": ("info", "Waiting for approval"),
    "approved": ("success", "Approved"),
    "rejected": ("danger", "Rejected"),
    "returned": ("warning", "Returned to you"),
    "reimbursed": ("success", "Paid"),
    "cancelled": ("neutral", "Cancelled"),
}
STEP_LABELS = {"manager": "Manager", "second": "Second level", "finance": "Finance"}
STEP_TONES = {
    "waiting": "neutral",
    "pending": "warning",
    "approved": "success",
    "rejected": "danger",
    "returned": "warning",
}
APPROVERS = (
    "expenses.claim.approve",
    "expenses.claim.approve_any",
    "expenses.claim.approve_finance",
)


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
        db, actor, active_module="expenses", active_section=section, crumbs=["Expenses", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _labels() -> dict[str, Any]:
    return {"statuses": STATUS, "step_labels": STEP_LABELS, "step_tones": STEP_TONES}


# ------------------------------------------------------------------ my expenses


@router.get("/expenses", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.my")
def my_expenses(
    request: Request, db: Db, actor: CurrentActor, tab: str = "", notice: str = ""
) -> HTMLResponse:
    employee_id = people.employee_of_user(db, actor.user_id)
    documents = service.mine(db, actor, employee_id) if employee_id else []
    tab = "advances" if tab == "advances" else "claims"
    notices = {
        "submitted": "Sent for approval.",
        "cancelled": "Cancelled.",
    }
    claims = [d for d in documents if d.kind == "claim"]
    advances = [d for d in documents if d.kind == "advance"]
    context = {
        "shell": _shell(db, actor, "My expenses"),
        "linked": employee_id is not None,
        "tab": tab,
        "rows": advances if tab == "advances" else claims,
        "waiting": sum((d.total for d in claims if d.status == "submitted"), Decimal(0)),
        "to_be_paid": sum((d.total for d in claims if d.status == "approved"), Decimal(0)),
        "advance_left": sum(
            (d.outstanding or Decimal(0) for d in advances if not d.settled), Decimal(0)
        ),
        "notice": notices.get(notice),
        "page_title": "My expenses",
        **_labels(),
    }
    return _render(request, "expenses/my.html", context)


def _new_context(db: Session, actor: Actor, kind: str, **extra: Any) -> dict[str, Any]:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None:
        raise NotFound()
    return {
        "shell": _shell(db, actor, "My expenses"),
        "kind": kind,
        "advances": service.open_advances(db, employee_id) if kind == "claim" else [],
        "values": {},
        "errors": {},
        "page_title": "Ask for an advance" if kind == "advance" else "New claim",
        "back_href": "/expenses",
        **extra,
    }


@router.get("/expenses/new", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def new_document(
    request: Request, db: Db, actor: CurrentActor, kind: str = "claim"
) -> HTMLResponse:
    kind = "advance" if kind == "advance" else "claim"
    return _render(request, "expenses/new.html", _new_context(db, actor, kind))


@router.post("/expenses/new", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def create_document(
    request: Request,
    db: Db,
    actor: CurrentActor,
    kind: FormStr = "claim",
    title: FormStr = "",
    amount: FormStr = "",
    advance_id: FormStr = "",
) -> Response:
    kind = "advance" if kind == "advance" else "claim"
    try:
        with db.begin_nested():
            document_id = service.create(
                db, actor, kind=kind, title=title, amount=amount, advance_id=_uuid(advance_id)
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"title": title, "amount": amount, "advance_id": advance_id}
        context = _new_context(db, actor, kind, values=values, errors=_errors(exc))
        return _render(request, "expenses/new.html", context, 422)
    return _see_other(f"/expenses/{document_id}")


# ------------------------------------------------------------------ one claim or advance


def _document_context(
    db: Session, actor: Actor, document_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    doc = service.get(db, actor, document_id)
    employee_id = people.employee_of_user(db, actor.user_id)
    mine = employee_id == doc.employee_id
    route = []
    if doc.can_edit:
        chain = org.approval_chain(db, doc.employee_id, 2)
        route = list(people.names(db, set(chain)).values()) if chain else []
    return {
        "shell": _shell(db, actor, "My expenses" if mine else "Approvals"),
        "doc": doc,
        "mine": mine,
        "categories": settings.list_categories(db, active_only=True),
        "cost_centres": org.cost_centre_choices(db),
        "projects": finance.project_link().choices(db),
        "default_cost_centre": org.cost_centre_of(db, doc.employee_id) if doc.can_edit else None,
        "advances": service.open_advances(db, doc.employee_id) if doc.can_edit else [],
        "config": settings.get_settings(db),
        "route": route,
        "can_view_receipts": bool(engine.check(db, actor.user_id, "expenses.claim.read")),
        "today": clock.today(),
        "values": {},
        "errors": {},
        "editing_line": "",
        "notice": None,
        "page_title": f"{doc.number} · {doc.title}",
        "back_href": "/expenses" if mine else "/expenses/approvals",
        **_labels(),
        **extra,
    }


@router.get("/expenses/{document_id:uuid}", response_class=HTMLResponse)
@requires_login
@screen("expenses.document")
def document_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: uuid.UUID,
    edit: str = "",
    notice: str = "",
) -> HTMLResponse:
    notices = {
        "saved": "Saved.",
        "receipt": "Receipt uploaded. It's checked for viruses before anyone opens it.",
    }
    context = _document_context(
        db, actor, document_id, editing_line=edit, notice=notices.get(notice)
    )
    return _render(request, "expenses/document.html", context)


def _document_problem(
    request: Request,
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    exc: DomainProblem,
    **extra: Any,
) -> HTMLResponse:
    context = _document_context(db, actor, document_id, errors=_errors(exc), **extra)
    return _render(request, "expenses/document.html", context, 422)


@router.post("/expenses/{document_id:uuid}/header", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def save_header(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: uuid.UUID,
    title: FormStr = "",
    amount: FormStr = "",
    advance_id: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            service.update_header(
                db, actor, document_id, title=title, amount=amount, advance_id=_uuid(advance_id)
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"title": title, "amount": amount, "advance_id": advance_id}
        return _document_problem(request, db, actor, document_id, exc, values=values)
    return _see_other(f"/expenses/{document_id}?notice=saved")


@router.post("/expenses/{document_id:uuid}/lines", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def save_line(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: uuid.UUID,
    line_id: FormStr = "",
    day: FormStr = "",
    category_id: FormStr = "",
    description: FormStr = "",
    cost_centre_id: FormStr = "",
    amount: FormStr = "",
    no_receipt_reason: FormStr = "",
    project_id: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            service.save_line(
                db,
                actor,
                document_id,
                _uuid(line_id),
                day=parse_day(day),
                category_id=_uuid(category_id),
                description=description,
                cost_centre_id=_uuid(cost_centre_id),
                amount=amount,
                no_receipt_reason=no_receipt_reason,
                project_id=project_id,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "day": day,
            "category_id": category_id,
            "description": description,
            "cost_centre_id": cost_centre_id,
            "amount": amount,
            "no_receipt_reason": no_receipt_reason,
            "project_id": project_id,
        }
        return _document_problem(
            request, db, actor, document_id, exc, values=values, editing_line=line_id or "new"
        )
    return _see_other(f"/expenses/{document_id}#lines")


@router.post("/expenses/lines/{line_id:uuid}/delete", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def delete_line(db: Db, actor: CurrentActor, line_id: uuid.UUID) -> Response:
    document_id = service.delete_line(db, actor, line_id)
    return _see_other(f"/expenses/{document_id}#lines")


@router.post("/expenses/lines/{line_id:uuid}/receipt", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def upload_receipt(
    db: Db, actor: CurrentActor, line_id: uuid.UUID, upload: ReceiptUpload
) -> Response:
    if upload.errors:
        raise ValidationFailed(field_errors=upload.errors)
    document_id = service.attach_receipt(
        db,
        actor,
        line_id,
        filename=upload.filename,
        declared_mime=upload.content_type,
        data=upload.data,
    )
    files.request_scan()
    return _see_other(f"/expenses/{document_id}?notice=receipt#lines")


@router.post("/expenses/{document_id:uuid}/submit", response_class=HTMLResponse)
@requires("expenses.claim.create")
@screen("expenses.document")
def submit(request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID) -> Response:
    try:
        with db.begin_nested():
            service.submit(db, actor, document_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _document_problem(request, db, actor, document_id, exc)
    return _see_other("/expenses?notice=submitted")


@router.post("/expenses/{document_id:uuid}/cancel", response_class=HTMLResponse)
@requires_login
@screen("expenses.document")
def cancel(db: Db, actor: CurrentActor, document_id: uuid.UUID) -> Response:
    service.cancel(db, actor, document_id)
    return _see_other("/expenses?notice=cancelled")


# ------------------------------------------------------------------ approvals


def _approvals_context(
    db: Session, actor: Actor, tab: str, selected: str, **extra: Any
) -> dict[str, Any]:
    if not any(engine.check(db, actor.user_id, p) for p in APPROVERS):
        raise PermissionDenied()
    tab = "decided" if tab == "decided" else "waiting"
    rows = service.inbox(db, actor) if tab == "waiting" else service.decided_by_me(db, actor)
    chosen = next((r for r in rows if str(r.id) == selected), rows[0] if rows else None)
    return {
        "shell": _shell(db, actor, "Approvals"),
        "tab": tab,
        "rows": rows,
        "waiting_count": len(service.inbox(db, actor)) if tab == "decided" else len(rows),
        "selected": service.get(db, actor, chosen.id) if chosen else None,
        "errors": {},
        "note": "",
        "amounts": {},
        "notice": None,
        "page_title": "Expense approvals",
        **_labels(),
        **extra,
    }


@router.get("/expenses/approvals", response_class=HTMLResponse)
@requires_login
@screen("expenses.approvals")
def approvals(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "",
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    notices = {
        "approve": "Approved.",
        "reject": "Rejected. They can see your note.",
        "return": "Returned for changes. They can see your note.",
    }
    context = _approvals_context(db, actor, tab, selected, notice=notices.get(notice))
    return _render(request, "expenses/approvals.html", context)


@router.post("/expenses/{document_id:uuid}/decide/{decision}", response_class=HTMLResponse)
@requires_login
@screen("expenses.approvals")
def decide(
    request: Request,
    db: Db,
    actor: CurrentActor,
    document_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
    line_id: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    line_amount: FormList = [],  # noqa: B006
) -> Response:
    corrections = {
        lid: amount
        for raw, amount in zip(line_id, line_amount, strict=False)
        if (lid := _uuid(raw)) is not None
    }
    try:
        with db.begin_nested():
            service.decide(
                db, actor, document_id, decision=decision, note=note, corrections=corrections
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _approvals_context(
            db,
            actor,
            "waiting",
            str(document_id),
            errors=_errors(exc),
            note=note,
            amounts={str(k): v for k, v in corrections.items()},
        )
        return _render(request, "expenses/approvals.html", context, 422)
    return _see_other(f"/expenses/approvals?notice={decision}")


# ------------------------------------------------------------------ payments


TABS = ("claims", "advances", "repayments", "paid")


def _payments_context(db: Session, actor: Actor, tab: str, **extra: Any) -> dict[str, Any]:
    tab = tab if tab in TABS else "claims"
    claims = service.to_pay(db, actor, "claim")
    advances = service.to_pay(db, actor, "advance")
    rows = {
        "claims": claims,
        "advances": advances,
        "repayments": service.repayments_due(db, actor),
        "paid": service.paid(db, actor),
    }[tab]
    return {
        "shell": _shell(db, actor, "Payments"),
        "tab": tab,
        "rows": rows,
        "counts": {"claims": len(claims), "advances": len(advances)},
        "payable": service.payable_ids(db, actor, [r.id for r in rows]),
        "journals": service.recent_journals(db, limit=20) if tab == "paid" else [],
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Expense payments",
        **_labels(),
        **extra,
    }


@router.get("/expenses/payments", response_class=HTMLResponse)
@requires("expenses.claim.reimburse")
@screen("expenses.payments")
def payments(
    request: Request, db: Db, actor: CurrentActor, tab: str = "", notice: str = ""
) -> HTMLResponse:
    notices = {
        "paid": "Marked as paid. Journals are queued for the ledger.",
        "repaid": "Repayment recorded.",
    }
    context = _payments_context(db, actor, tab, notice=notices.get(notice))
    return _render(request, "expenses/payments.html", context)


@router.post("/expenses/payments/pay", response_class=HTMLResponse)
@requires("expenses.claim.reimburse")
@screen("expenses.payments")
def pay(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: FormStr = "claims",
    document_id: FormList = [],  # noqa: B006
    paid_on: FormStr = "",
    method: FormStr = "",
    reference: FormStr = "",
) -> Response:
    ids = [i for i in (_uuid(d) for d in document_id) if i is not None]
    try:
        with db.begin_nested():
            service.pay(
                db, actor, ids, paid_on=parse_day(paid_on), method=method, reference=reference
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "paid_on": paid_on,
            "method": method,
            "reference": reference,
            "chosen": set(document_id),
        }
        context = _payments_context(db, actor, tab, values=values, errors=_errors(exc))
        return _render(request, "expenses/payments.html", context, 422)
    return _see_other(f"/expenses/payments?tab={tab}&notice=paid")


@router.post("/expenses/advances/{advance_id:uuid}/repayment", response_class=HTMLResponse)
@requires("expenses.claim.reimburse")
@screen("expenses.payments")
def repayment(
    request: Request,
    db: Db,
    actor: CurrentActor,
    advance_id: uuid.UUID,
    repay_amount: FormStr = "",
    received_on: FormStr = "",
    repay_method: FormStr = "",
    repay_reference: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            service.record_repayment(
                db,
                actor,
                advance_id,
                amount=repay_amount,
                received_on=parse_day(received_on),
                method=repay_method,
                reference=repay_reference,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "advance": str(advance_id),
            "repay_amount": repay_amount,
            "received_on": received_on,
            "repay_method": repay_method,
            "repay_reference": repay_reference,
        }
        context = _payments_context(db, actor, "repayments", values=values, errors=_errors(exc))
        return _render(request, "expenses/payments.html", context, 422)
    return _see_other("/expenses/payments?tab=repayments&notice=repaid")


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Settings"),
        "config": settings.get_settings(db),
        "categories": settings.list_categories(db),
        "limits": settings.person_limits(db),
        "people": [
            (e.id, e.full_name)
            for e in people.page_in_scope(db, actor, "hr.employee.read", size=500)[0]
        ],
        "editing": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Expense settings",
        **extra,
    }


@router.get("/expenses/settings", response_class=HTMLResponse)
@requires("expenses.settings.manage")
@screen("expenses.settings")
def expense_settings(
    request: Request, db: Db, actor: CurrentActor, notice: str = ""
) -> HTMLResponse:
    context = _settings_context(db, actor, notice="Saved." if notice else None)
    return _render(request, "expenses/settings.html", context)


@router.post("/expenses/settings", response_class=HTMLResponse)
@requires("expenses.settings.manage")
@screen("expenses.settings")
def save_settings(
    request: Request,
    db: Db,
    actor: CurrentActor,
    second_level_above: FormStr = "",
    finance_above: FormStr = "",
    weekly_limit: FormStr = "",
    monthly_limit: FormStr = "",
    advances_account: FormStr = "",
    bank_account: FormStr = "",
) -> Response:
    values = {
        "second_level_above": second_level_above,
        "finance_above": finance_above,
        "weekly_limit": weekly_limit,
        "monthly_limit": monthly_limit,
        "advances_account": advances_account,
        "bank_account": bank_account,
    }
    try:
        with db.begin_nested():
            settings.save_settings(db, actor, **values)
    except (ValidationFailed, Conflict) as exc:
        context = _settings_context(db, actor, errors=_errors(exc), values=values, editing="rules")
        return _render(request, "expenses/settings.html", context, 422)
    return _see_other("/expenses/settings?notice=1#rules")


@router.post("/expenses/settings/categories", response_class=HTMLResponse)
@requires("expenses.settings.manage")
@screen("expenses.settings")
def save_category(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category_id: FormStr = "",
    name: FormStr = "",
    gl_account: FormStr = "",
    claim_limit: FormStr = "",
    receipt_above: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            settings.save_category(
                db,
                actor,
                _uuid(category_id),
                name=name,
                gl_account=gl_account,
                claim_limit=claim_limit,
                receipt_above=receipt_above,
                is_active=bool(is_active) or not category_id,
            )
    except (ValidationFailed, Conflict) as exc:
        values = {
            "name": name,
            "gl_account": gl_account,
            "claim_limit": claim_limit,
            "receipt_above": receipt_above,
        }
        context = _settings_context(
            db,
            actor,
            errors=_errors(exc),
            values=values,
            editing=f"category:{category_id or 'new'}",
        )
        return _render(request, "expenses/settings.html", context, 422)
    return _see_other("/expenses/settings?notice=1#categories")


@router.post("/expenses/settings/limits", response_class=HTMLResponse)
@requires("expenses.settings.manage")
@screen("expenses.settings")
def save_person_limit(
    request: Request,
    db: Db,
    actor: CurrentActor,
    limit_employee_id: FormStr = "",
    person_weekly: FormStr = "",
    person_monthly: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            settings.save_person_limit(
                db,
                actor,
                _uuid(limit_employee_id),
                weekly_limit=person_weekly,
                monthly_limit=person_monthly,
            )
    except (ValidationFailed, Conflict) as exc:
        values = {
            "limit_employee_id": limit_employee_id,
            "person_weekly": person_weekly,
            "person_monthly": person_monthly,
        }
        context = _settings_context(db, actor, errors=_errors(exc), values=values, editing="limits")
        return _render(request, "expenses/settings.html", context, 422)
    return _see_other("/expenses/settings?notice=1#limits")
