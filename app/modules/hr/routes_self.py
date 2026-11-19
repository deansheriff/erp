"""Everyone's HR pages (brief 9.2 and 9.3): the colleague directory and the self-service
profile. Every signed-in person may open them; the services decide what they see."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, requires_login, screen
from app.authz.fields import FieldClass
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import checklists, directory, documents, records
from app.modules.hr import service as hr
from app.modules.hr.checklists import ASSIGNEE_LABELS
from app.modules.hr.fields import fields_of
from app.web.access import require_actor
from app.web.forms import all_fields
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
Fields = Annotated[dict[str, str], Depends(all_fields)]

TABS = ("details", "documents", "requests", "tasks")
REQUEST_CLASSES = {"statutory": FieldClass.STATUTORY, "banking": FieldClass.BANKING}
NOTICES = {
    "saved": "Your details are saved.",
    "requested": "Request sent to HR. You'll see their answer here.",
    "cancelled": "Request cancelled.",
    "task": "Task updated.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: ValidationFailed | Conflict | PermissionDenied) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


# ------------------------------------------------------------------ directory


@router.get("/directory", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.directory")
def colleague_directory(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    department: str = "",
    branch: str = "",
    page: int = 1,
) -> HTMLResponse:
    result = directory.search(
        db, actor, q=q, department=department, branch=branch, page=max(page, 1)
    )
    context = {
        "shell": build_shell(
            db, actor, active_module="hr", active_section="Directory", crumbs=["Directory"]
        ),
        "result": result,
        "filters": {"q": q, "department": department, "branch": branch},
        "departments": directory.department_choices(db),
        "branches": hr.branch_choices(db),
        "page_title": "Directory",
    }
    return _render(request, "hr/directory.html", context)


# ------------------------------------------------------------------ my profile


def _me_context(db: Session, actor: Actor, tab: str, **extra: Any) -> dict[str, Any]:
    employee_id = hr.employee_of_user(db, actor.user_id)
    record = records.get_record(db, actor, employee_id) if employee_id else None
    values = extra.pop("values", None)
    if values is None and record is not None:
        values = {f.field.name: f.value for s in record.sections for f in s.fields}
    return {
        "shell": build_shell(
            db, actor, active_module="home", active_section=None, crumbs=["My profile"]
        ),
        "record": record,
        "tab": tab if tab in TABS else "details",
        "documents": documents.list_documents(db, actor, employee_id) if employee_id else [],
        "requests": records.my_requests(db, actor),
        "pending": {r.field_class for r in records.my_requests(db, actor) if r.status == "pending"},
        "tasks": checklists.my_open_tasks(db, actor),
        "request_fields": {k: fields_of(c) for k, c in REQUEST_CLASSES.items()},
        "can_request": bool(record) and records.can_request_change(db, actor, employee_id),
        "assignee_labels": ASSIGNEE_LABELS,
        "values": values or {},
        "errors": {},
        "editing": "",
        "page_title": "My profile",
        **extra,
    }


@router.get("/me", response_class=HTMLResponse)
@requires_login
@screen("me.profile")
def my_profile(
    request: Request, db: Db, actor: CurrentActor, tab: str = "details", notice: str = ""
) -> HTMLResponse:
    context = _me_context(db, actor, tab, notice=NOTICES.get(notice))
    return _render(request, "hr/me.html", context)


def _my_employee(db: Session, actor: Actor) -> uuid.UUID:
    employee_id = hr.employee_of_user(db, actor.user_id)
    if employee_id is None:
        raise NotFound()
    return employee_id


@router.post("/me/details", response_class=HTMLResponse)
@requires_login
@screen("me.profile")
def save_my_details(request: Request, db: Db, actor: CurrentActor, values: Fields) -> Response:
    employee_id = _my_employee(db, actor)
    record = records.get_record(db, actor, employee_id)
    editable = {f.field.name for s in record.sections for f in s.fields if f.editable}
    try:
        with db.begin_nested():
            records.update_fields(
                db, actor, employee_id, {k: v for k, v in values.items() if k in editable}
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _me_context(
            db, actor, "details", values=values, errors=_errors(exc), editing="details"
        )
        return _render(request, "hr/me.html", context, 422)
    return RedirectResponse("/me?notice=saved", status_code=303)


@router.post("/me/requests/{field_class}", response_class=HTMLResponse)
@requires("hr.profile.request_change")
@screen("me.profile")
def request_change(
    request: Request, db: Db, actor: CurrentActor, field_class: str, values: Fields
) -> Response:
    target = REQUEST_CLASSES.get(field_class)
    if target is None:
        raise NotFound()
    try:
        with db.begin_nested():
            records.request_change(db, actor, target, values)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _me_context(
            db, actor, "details", values=values, errors=_errors(exc), editing=field_class
        )
        return _render(request, "hr/me.html", context, 422)
    return RedirectResponse("/me?tab=requests&notice=requested", status_code=303)


@router.post("/me/requests/{request_id}/cancel", response_class=HTMLResponse)
@requires("hr.profile.request_change")
@screen("me.profile")
def cancel_request(db: Db, actor: CurrentActor, request_id: uuid.UUID) -> Response:
    records.cancel_request(db, actor, request_id)
    return RedirectResponse("/me?tab=requests&notice=cancelled", status_code=303)
