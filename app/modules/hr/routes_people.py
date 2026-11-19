"""HR web routes for Phase 6: the employee profile, the add/edit form, documents,
invitations, onboarding and exits. Routes translate forms and render; the HR services do
the work and enforce field-level access (ADR 0017)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.audit import queries as audit
from app.authz import engine
from app.authz.declarations import requires, screen
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.hr import checklists, documents, exits, records
from app.modules.hr import service as hr
from app.modules.hr.checklists import ASSIGNEE_LABELS
from app.modules.hr.fields import BY_NAME
from app.modules.hr.files import DOCUMENT_PURPOSES
from app.modules.hr.schemas import EmployeeRecord, PersonInput
from app.web.access import require_actor
from app.web.forms import all_fields, field_errors, parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
Fields = Annotated[dict[str, str], Depends(all_fields)]
DocumentUpload = Annotated[
    ReceivedUpload, Depends(upload_body(max(p.max_bytes for p in DOCUMENT_PURPOSES.values())))
]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

STATUSES = {
    "active": ("success", "Active"),
    "on_leave": ("info", "On leave"),
    "suspended": ("danger", "Suspended"),
    "terminated": ("neutral", "Left"),
}
KIND_LABELS = {"primary": "Primary", "acting": "Acting", "interim": "Interim"}
CLASS_LABELS = {
    "personal": "Personal",
    "statutory": "Statutory",
    "banking": "Bank",
    "compensation": "Pay",
    "sensitive": "Sensitive",
}
NOTICES = {
    "saved": "Saved.",
    "added": "Person added. Their employee number is shown below.",
    "invited": "Invitation sent. They get the Employee role when they set up their account.",
    "onboarding": "Onboarding started.",
    "exit": "Exit recorded. Access ends automatically after the last working day.",
    "exit_cancelled": "Exit cancelled.",
    "document": "Document added. It can be opened once the virus check finishes.",
    "replaced": "New version uploaded. It replaces the old one once the virus check passes.",
    "deleted": "Document deleted.",
}
ACTIONS = {
    "hr.employee.create": "Added to the organisation",
    "hr.employee.update": "Directory details changed",
    "hr.employee.update_details": "HR details changed",
    "hr.employee.invite": "Invited to sign in",
    "hr.document.add": "Document added",
    "hr.document.replace": "Document replaced",
    "hr.document.delete": "Document deleted",
    "hr.onboarding.start": "Onboarding started",
    "hr.offboarding.start": "Offboarding started",
    "hr.offboarding.cancel": "Offboarding cancelled",
    "hr.checklist_task.done": "Checklist task done",
    "hr.checklist_task.reopen": "Checklist task reopened",
    "hr.exit.record": "Exit recorded",
    "hr.exit.cancel": "Exit cancelled",
    "hr.exit.revoke": "Access revoked after the last working day",
    "hr.change_request.create": "Change requested",
    "hr.change_request.approve": "Change request approved",
    "hr.change_request.reject": "Change request rejected",
    "hr.change_request.cancel": "Change request cancelled",
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


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = records.resource(employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def _shell(db: Session, actor: Actor, crumbs: list[str]) -> Any:
    return build_shell(
        db, actor, active_module="hr", active_section="People", crumbs=["HR", *crumbs]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ add / edit


def _form_context(
    db: Session, actor: Actor, record: EmployeeRecord | None, **extra: Any
) -> dict[str, Any]:
    directory_editable = record is None or (
        _can(db, actor, "hr.employee.update", record.id) and not record.access.is_self
    )
    values = extra.pop("values", None)
    if values is None:
        values = {}
        if record is not None:
            values = {
                "first_name": record.first_name,
                "last_name": record.last_name,
                "other_names": record.other_names or "",
                "work_email": record.work_email or "",
                "branch_id": str(record.branch_id or ""),
                "user_id": str(record.user_id or ""),
                "status": record.status,
                **{f.field.name: f.value for s in record.sections for f in s.fields},
            }
    if record is None:
        # A new person: every class the actor may set on anyone in the organisation.
        sections = records.blank_sections(db, actor)
    else:
        sections = [s for s in record.sections if s.editable]
    name = record.full_name if record else "Add person"
    return {
        "shell": _shell(db, actor, ["People", name]),
        "record": record,
        "sections": sections,
        "directory_editable": directory_editable,
        "values": values,
        "errors": {},
        "branches": hr.branch_choices(db, keep=record.branch_id if record else None),
        "accounts": hr.accounts_without_person(db, keep=record.user_id if record else None),
        "statuses": {k: v for k, v in STATUSES.items() if k != "terminated"},
        "page_title": f"Edit {name}" if record else name,
        "back_href": f"/hr/people/{record.id}" if record else "/hr/people",
        **extra,
    }


def _person_input(values: dict[str, str], *, new: bool) -> PersonInput:
    return PersonInput(
        first_name=values.get("first_name", ""),
        last_name=values.get("last_name", ""),
        other_names=values.get("other_names") or None,
        work_email=values.get("work_email") or None,
        branch_id=_uuid(values.get("branch_id")),
        user_id=_uuid(values.get("user_id")),
        status=None if new else (values.get("status") or None),
    )


@router.get("/hr/people/new", response_class=HTMLResponse)
@requires("hr.employee.create")
@screen("hr.employee_form")
def new_person(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "hr/employee_form.html", _form_context(db, actor, None))


@router.post("/hr/people/new", response_class=HTMLResponse)
@requires("hr.employee.create")
@screen("hr.employee_form")
def create_person(request: Request, db: Db, actor: CurrentActor, values: Fields) -> Response:
    try:
        with db.begin_nested():
            person_id = hr.add_person(db, actor, _person_input(values, new=True))
            records.update_fields(
                db, actor, person_id, {k: v for k, v in values.items() if k in BY_NAME}
            )
    except ValidationError as exc:
        context = _form_context(db, actor, None, values=values, errors=field_errors(exc))
        return _render(request, "hr/employee_form.html", context, 422)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _form_context(db, actor, None, values=values, errors=_errors(exc))
        return _render(request, "hr/employee_form.html", context, 422)
    return _see_other(f"/hr/people/{person_id}?notice=added")


@router.get("/hr/people/{person_id}/edit", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee_form")
def edit_person(
    request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID
) -> HTMLResponse:
    record = records.get_record(db, actor, person_id)
    context = _form_context(db, actor, record)
    if not context["directory_editable"] and not context["sections"]:
        raise PermissionDenied()
    return _render(request, "hr/employee_form.html", context)


@router.post("/hr/people/{person_id}/edit", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee_form")
def save_person(
    request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID, values: Fields
) -> Response:
    record = records.get_record(db, actor, person_id)
    editable = {f.field.name for s in record.sections for f in s.fields if f.editable}
    try:
        with db.begin_nested():
            if "first_name" in values:
                hr.update_person(db, actor, person_id, _person_input(values, new=False))
            records.update_fields(
                db, actor, person_id, {k: v for k, v in values.items() if k in editable}
            )
    except ValidationError as exc:
        context = _form_context(db, actor, record, values=values, errors=field_errors(exc))
        return _render(request, "hr/employee_form.html", context, 422)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _form_context(db, actor, record, values=values, errors=_errors(exc))
        return _render(request, "hr/employee_form.html", context, 422)
    return _see_other(f"/hr/people/{person_id}?notice=saved")


# ------------------------------------------------------------------ profile


def _profile_context(
    db: Session, actor: Actor, record: EmployeeRecord, **extra: Any
) -> dict[str, Any]:
    eid = record.id
    can_update = _can(db, actor, "hr.employee.update", eid) and not record.access.is_self
    left = record.status == "terminated"
    timeline = (
        [
            (
                e.occurred_at,
                ACTIONS.get(e.action, e.action),
                (e.actor_label or "System").split(" <")[0],  # "Name <email>" → "Name"
            )
            for e in audit.for_entity(db, "employee", str(eid))
        ]
        if can_update
        else []
    )
    return {
        "shell": _shell(db, actor, ["People", record.full_name]),
        "record": record,
        "documents": documents.list_documents(db, actor, eid),
        "upload_categories": documents.uploadable_categories(db, actor, eid) if not left else [],
        "checklists": checklists.for_employee(db, actor, eid),
        "exit": exits.exit_for(db, eid) if _can(db, actor, "hr.exit.manage") else None,
        "timeline": timeline,
        "can_edit": can_update or any(s.editable for s in record.sections),
        "can_invite": not left
        and record.user_id is None
        and _can(db, actor, "identity.user.invite"),
        "can_onboard": not left and _can(db, actor, "hr.checklist.manage"),
        "can_exit": not left and not record.access.is_self and _can(db, actor, "hr.exit.manage"),
        "hidden_classes": [
            CLASS_LABELS[c.value]
            for c in records.RECORD_CLASSES
            if c not in record.access.read and c.value in ("personal", "statutory", "banking")
        ],
        "statuses": STATUSES,
        "kind_labels": KIND_LABELS,
        "class_labels": CLASS_LABELS,
        "assignee_labels": ASSIGNEE_LABELS,
        "exit_values": {},
        "exit_errors": {},
        "document_errors": {},
        "errors": {},
        "page_title": record.full_name,
        "back_href": "/hr/people",
        **extra,
    }


@router.get("/hr/people/{person_id}", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee")
def profile(
    request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    record = records.get_record(db, actor, person_id)
    context = _profile_context(db, actor, record, notice=NOTICES.get(notice))
    return _render(request, "hr/employee.html", context)


def _profile_problem(
    request: Request, db: Session, actor: Actor, person_id: uuid.UUID, **extra: Any
) -> HTMLResponse:
    record = records.get_record(db, actor, person_id)
    return _render(request, "hr/employee.html", _profile_context(db, actor, record, **extra), 422)


@router.post("/hr/people/{person_id}/invite", response_class=HTMLResponse)
@requires("identity.user.invite")
@screen("hr.employee")
def invite(request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID) -> Response:
    try:
        with db.begin_nested():
            exits.invite(db, actor, person_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _profile_problem(request, db, actor, person_id, errors=_errors(exc))
    return _see_other(f"/hr/people/{person_id}?notice=invited")


@router.post("/hr/people/{person_id}/onboarding", response_class=HTMLResponse)
@requires("hr.checklist.manage")
@screen("hr.employee")
def start_onboarding(
    request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID
) -> Response:
    try:
        checklists.start_onboarding(db, actor, person_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _profile_problem(request, db, actor, person_id, errors=_errors(exc))
    return _see_other(f"/hr/people/{person_id}?notice=onboarding")


@router.post("/hr/people/{person_id}/exit", response_class=HTMLResponse)
@requires("hr.exit.manage")
@screen("hr.employee")
def record_exit(
    request: Request,
    db: Db,
    actor: CurrentActor,
    person_id: uuid.UUID,
    kind: FormStr = "",
    reason: FormStr = "",
    notice_date: FormStr = "",
    last_working_day: FormStr = "",
    final_pay_due: FormStr = "",
) -> Response:
    values = {
        "kind": kind,
        "reason": reason,
        "notice_date": notice_date,
        "last_working_day": last_working_day,
        "final_pay_due": final_pay_due,
    }
    try:
        with db.begin_nested():
            exits.record_exit(
                db,
                actor,
                person_id,
                kind=kind,
                reason=reason,
                notice_date=parse_day(notice_date),
                last_working_day=parse_day(last_working_day),
                final_pay_due=bool(final_pay_due),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _profile_problem(
            request,
            db,
            actor,
            person_id,
            exit_values=values,
            exit_errors=_errors(exc),
            exit_open=True,
        )
    return _see_other(f"/hr/people/{person_id}?notice=exit")


@router.post("/hr/exits/{exit_id}/cancel", response_class=HTMLResponse)
@requires("hr.exit.manage")
@screen("hr.employee")
def cancel_exit(db: Db, actor: CurrentActor, exit_id: uuid.UUID) -> Response:
    person_id = exits.cancel_exit(db, actor, exit_id)
    return _see_other(f"/hr/people/{person_id}?notice=exit_cancelled")


# ------------------------------------------------------------------ documents


@router.post("/hr/people/{person_id}/documents", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee")
def add_document(
    request: Request, db: Db, actor: CurrentActor, person_id: uuid.UUID, upload: DocumentUpload
) -> Response:
    try:
        if upload.errors:
            raise ValidationFailed(field_errors=upload.errors)
        with db.begin_nested():
            documents.add_document(
                db,
                actor,
                person_id,
                category_id=_uuid(upload.fields.get("category_id")),
                title=upload.fields.get("title", ""),
                filename=upload.filename,
                declared_mime=upload.content_type,
                data=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _profile_problem(
            request, db, actor, person_id, document_errors=_errors(exc), document_open=True
        )
    files.request_scan()
    return _see_other(f"/hr/people/{person_id}?notice=document#documents")


@router.post("/hr/documents/{document_id}/replace", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee")
def replace_document(
    request: Request, db: Db, actor: CurrentActor, document_id: uuid.UUID, upload: DocumentUpload
) -> Response:
    person_id = documents.employee_of(db, document_id)
    if person_id is None:
        raise NotFound()
    try:
        if upload.errors:
            raise ValidationFailed(field_errors=upload.errors)
        with db.begin_nested():
            documents.replace_document(
                db,
                actor,
                document_id,
                filename=upload.filename,
                declared_mime=upload.content_type,
                data=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _profile_problem(request, db, actor, person_id, document_errors=_errors(exc))
    files.request_scan()
    return _see_other(f"/hr/people/{person_id}?notice=replaced#documents")


@router.post("/hr/documents/{document_id}/delete", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.employee")
def delete_document(db: Db, actor: CurrentActor, document_id: uuid.UUID) -> Response:
    person_id = documents.delete_document(db, actor, document_id)
    return _see_other(f"/hr/people/{person_id}?notice=deleted#documents")
