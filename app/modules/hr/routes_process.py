"""HR processes: the change-request inbox, onboarding and offboarding checklists, and HR
settings (employee numbers, document categories, checklist templates)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core import numbering
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import checklists, documents, records
from app.modules.hr.checklists import ASSIGNEE_LABELS
from app.modules.hr.models import DOCUMENT_CLASSES
from app.modules.hr.service import EMPLOYEE_SEQUENCE
from app.org import service as org
from app.web.access import require_actor
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

REQUEST_STATUSES = {
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
}
CHECKLIST_TABS = {
    "onboarding": "Onboarding",
    "offboarding": "Offboarding",
    "mine": "Assigned to me",
}
CLASS_LABELS = {
    "personal": "Personal",
    "statutory": "Statutory",
    "banking": "Bank",
    "compensation": "Pay",
    "sensitive": "Sensitive",
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
        db, actor, active_module="hr", active_section=section, crumbs=["HR", section]
    )


def _local_path(value: str, default: str) -> str:
    """Only same-site paths are followed after a form (no open redirects)."""
    return value if value.startswith("/") and not value.startswith("//") else default


# ------------------------------------------------------------------ change requests


def _require_decider(db: Session, actor: Actor) -> None:
    if not any(
        engine.check(db, actor.user_id, f"hr.employee.{c}.update") for c in ("statutory", "banking")
    ):
        raise PermissionDenied()


def _requests_context(
    db: Session, actor: Actor, status: str, selected: str, **extra: Any
) -> dict[str, Any]:
    status = status if status in REQUEST_STATUSES else "pending"
    rows = records.list_requests(db, actor, status=status)
    chosen = next((r for r in rows if str(r.id) == selected), rows[0] if rows else None)
    return {
        "shell": _shell(db, actor, "Change requests"),
        "rows": rows,
        "selected": chosen,
        "status": status,
        "statuses": REQUEST_STATUSES,
        "pending_count": records.count_pending(db, actor),
        "errors": {},
        "page_title": "Change requests",
        **extra,
    }


@router.get("/hr/change-requests", response_class=HTMLResponse)
@requires_login
@screen("hr.change_requests")
def change_requests(
    request: Request,
    db: Db,
    actor: CurrentActor,
    status: str = "pending",
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    _require_decider(db, actor)
    notices = {"approved": "Approved and applied.", "rejected": "Rejected. They can see why."}
    context = _requests_context(db, actor, status, selected, notice=notices.get(notice))
    return _render(request, "hr/change_requests.html", context)


@router.post("/hr/change-requests/{request_id}/{decision}", response_class=HTMLResponse)
@requires_login
@screen("hr.change_requests")
def decide_request(
    request: Request,
    db: Db,
    actor: CurrentActor,
    request_id: uuid.UUID,
    decision: str,
    note: FormStr = "",
) -> Response:
    _require_decider(db, actor)
    if decision not in {"approve", "reject"}:
        raise PermissionDenied()
    try:
        with db.begin_nested():
            records.decide(db, actor, request_id, approve=decision == "approve", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _requests_context(
            db, actor, "pending", str(request_id), errors=_errors(exc), note=note
        )
        return _render(request, "hr/change_requests.html", context, 422)
    outcome = "approved" if decision == "approve" else "rejected"
    return RedirectResponse(f"/hr/change-requests?notice={outcome}", status_code=303)


# ------------------------------------------------------------------ checklists


@router.get("/hr/checklists", response_class=HTMLResponse)
@requires_login
@screen("hr.checklists")
def checklist_board(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "",
    overdue: int = 0,
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    manage = bool(engine.check(db, actor.user_id, "hr.checklist.manage"))
    tab = tab if tab in CHECKLIST_TABS else ("onboarding" if manage else "mine")
    if tab == "mine":
        rows = checklists.list_open(db, actor, mine=True, overdue=bool(overdue))
    else:
        rows = checklists.list_open(db, actor, kind=tab, overdue=bool(overdue))
    chosen = next((c for c in rows if str(c.id) == selected), rows[0] if rows else None)
    context = {
        "shell": _shell(db, actor, "Joiners & leavers"),
        "rows": rows,
        "selected": chosen,
        "tab": tab,
        "tabs": CHECKLIST_TABS if manage else {"mine": CHECKLIST_TABS["mine"]},
        "mine_count": len(checklists.my_open_tasks(db, actor)),
        "overdue": bool(overdue),
        "assignee_labels": ASSIGNEE_LABELS,
        "notice": "Task updated." if notice == "task" else None,
        "page_title": "Joiners & leavers",
    }
    return _render(request, "hr/checklists.html", context)


@router.post("/hr/checklist-tasks/{task_id}", response_class=HTMLResponse)
@requires_login
@screen("hr.checklists")
def toggle_task(
    db: Db,
    actor: CurrentActor,
    task_id: uuid.UUID,
    done: FormStr = "",
    return_to: FormStr = "",
) -> Response:
    checklist_id = checklists.set_done(db, actor, task_id, done=done == "1")
    default = f"/hr/checklists?selected={checklist_id}&notice=task"
    return RedirectResponse(_local_path(return_to, default), status_code=303)


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    sequence = db.get(numbering.NumberSequence, EMPLOYEE_SEQUENCE)
    return {
        "shell": _shell(db, actor, "HR settings"),
        "sequence": sequence,
        "next_number": numbering.preview(db, EMPLOYEE_SEQUENCE),
        "categories": documents.categories(db),
        "class_labels": CLASS_LABELS,
        "document_classes": DOCUMENT_CLASSES,
        "templates_by_kind": {
            kind: [t for t in checklists.list_templates(db) if t.kind == kind]
            for kind in ("onboarding", "offboarding")
        },
        "assignee_labels": ASSIGNEE_LABELS,
        "departments": [(d.id, d.name) for d in org.list_departments(db)],
        "positions": org.position_choices(db),
        "errors": {},
        "editing": "",
        "page_title": "HR settings",
        **extra,
    }


@router.get("/hr/settings", response_class=HTMLResponse)
@requires("hr.settings.manage")
@screen("hr.settings")
def hr_settings(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(db, actor, editing=edit, notice="Saved." if notice else None)
    return _render(request, "hr/settings.html", context)


@router.post("/hr/settings/numbers", response_class=HTMLResponse)
@requires("hr.settings.manage")
@screen("hr.settings")
def save_numbers(
    request: Request, db: Db, actor: CurrentActor, prefix: FormStr = "", padding: FormStr = ""
) -> Response:
    try:
        records.configure_numbers(db, actor, prefix=prefix, padding=padding)
    except ValidationFailed as exc:
        context = _settings_context(db, actor, errors=_errors(exc), editing="numbers")
        return _render(request, "hr/settings.html", context, 422)
    return RedirectResponse("/hr/settings?notice=saved", status_code=303)


@router.post("/hr/settings/categories", response_class=HTMLResponse)
@requires("hr.settings.manage")
@screen("hr.settings")
def save_category(
    request: Request,
    db: Db,
    actor: CurrentActor,
    category_id: FormStr = "",
    name: FormStr = "",
    field_class: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            documents.save_category(
                db,
                actor,
                _uuid(category_id),
                name=name,
                field_class=field_class,
                is_active=bool(is_active) or not category_id,
            )
    except (ValidationFailed, Conflict) as exc:
        context = _settings_context(
            db, actor, errors=_errors(exc), editing=f"category:{category_id or 'new'}"
        )
        return _render(request, "hr/settings.html", context, 422)
    return RedirectResponse("/hr/settings?notice=saved#categories", status_code=303)


@router.post("/hr/settings/templates", response_class=HTMLResponse)
@requires("hr.settings.manage")
@screen("hr.settings")
def save_template(
    request: Request,
    db: Db,
    actor: CurrentActor,
    template_id: FormStr = "",
    kind: FormStr = "",
    name: FormStr = "",
    target: FormStr = "",
    is_active: FormStr = "",
    task_title: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    task_assignee: FormList = [],  # noqa: B006
    task_due: FormList = [],  # noqa: B006
) -> Response:
    department_id = (
        _uuid(target.removeprefix("department:")) if target.startswith("department:") else None
    )
    position_id = (
        _uuid(target.removeprefix("position:")) if target.startswith("position:") else None
    )
    tasks = list(zip(task_title, task_assignee, task_due, strict=False))
    try:
        with db.begin_nested():
            checklists.save_template(
                db,
                actor,
                _uuid(template_id),
                kind=kind,
                name=name,
                department_id=department_id,
                position_id=position_id,
                is_active=bool(is_active) or not template_id,
                tasks=tasks,
            )
    except (ValidationFailed, Conflict) as exc:
        context = _settings_context(
            db, actor, errors=_errors(exc), editing=f"template:{template_id or kind}"
        )
        return _render(request, "hr/settings.html", context, 422)
    return RedirectResponse("/hr/settings?notice=saved#templates", status_code=303)
