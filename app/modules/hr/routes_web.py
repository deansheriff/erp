"""HR web routes for Phase 4: org chart, positions, departments, structure setup and
people. Routes translate forms and render; the org and HR services do the work."""

import uuid
from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as hr
from app.org import service as org
from app.org.schemas import PositionNode
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]

KIND_LABELS = {"primary": "Primary", "acting": "Acting", "interim": "Interim"}
REFERENCE_TABS = {
    "branches": ("Branches", "branch"),
    "cost_centres": ("Cost centres", "cost centre"),
    "grades": ("Grades", "grade"),
}
DomainProblem = ValidationFailed | Conflict | PermissionDenied


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


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _shell(db: Session, actor: Actor, section: str, crumbs: list[str]) -> Any:
    return build_shell(
        db, actor, active_module="hr", active_section=section, crumbs=["HR", *crumbs]
    )


# ------------------------------------------------------------------ org chart


def _chart_context(
    db: Session,
    actor: Actor,
    *,
    department: str = "",
    vacant: bool = False,
    q: str = "",
    selected: str = "",
    **extra: Any,
) -> dict[str, Any]:
    nodes = org.list_positions(db)
    holder_ids = {n.holder.employee_id for n in nodes if n.holder}
    names = hr.names(db, holder_ids)
    children: dict[uuid.UUID | None, list[PositionNode]] = defaultdict(list)
    ids = {n.id for n in nodes}
    for n in nodes:
        children[n.parent_id if n.parent_id in ids else None].append(n)
    dept_id = _uuid(department)
    term = q.strip().lower()
    filtered = bool(dept_id or vacant or term)
    under = org.departments_under(db, dept_id) if dept_id else set()
    matches = (
        [
            n
            for n in nodes
            if (not dept_id or n.department_id in under)
            and (not vacant or n.holder is None)
            and (
                not term
                or term in n.title.lower()
                or (n.holder and term in names.get(n.holder.employee_id, "").lower())
            )
        ]
        if filtered
        else []
    )
    sel_id = _uuid(selected)
    return {
        "shell": _shell(db, actor, "Org chart", ["Org chart"]),
        "nodes": nodes,
        "children": children,
        "names": names,
        "matches": matches,
        "filtered": filtered,
        "filters": {"department": department, "vacant": vacant, "q": q},
        "departments": org.list_departments(db),
        "selected": org.position_detail(db, sel_id) if sel_id and sel_id in ids else None,
        "kind_labels": KIND_LABELS,
        "can_manage": _can(db, actor, "org.structure.manage"),
        "can_assign": _can(db, actor, "org.assignment.manage"),
        "vacant_count": sum(1 for n in nodes if n.holder is None),
        "today": clock.today(),
        "page_title": "Org chart",
        **_position_form_context(db),
        **extra,
    }


def _position_form_context(db: Session) -> dict[str, Any]:
    return {
        "position_choices": org.position_choices(db),
        "department_choices": [
            (d.id, f"{'— ' * d.depth}{d.name}") for d in org.list_departments(db)
        ],
        "grade_choices": [g for g in org.list_reference(db, "grades") if g.is_active],
        "branch_choices": [b for b in org.list_reference(db, "branches") if b.is_active],
        "position_errors": {},
        "position_values": {},
    }


@router.get("/hr/org-chart", response_class=HTMLResponse)
@requires("org.structure.read")
@screen("hr.org_chart")
def org_chart(
    request: Request,
    db: Db,
    actor: CurrentActor,
    department: str = "",
    vacant: int = 0,
    q: str = "",
    selected: str = "",
) -> HTMLResponse:
    context = _chart_context(
        db, actor, department=department, vacant=bool(vacant), q=q, selected=selected
    )
    return _render(request, "hr/org_chart.html", context)


def _position_fields(
    title: str, department_id: str, parent_id: str, grade_id: str, branch_id: str
) -> dict[str, Any]:
    return {
        "title": title,
        "department_id": _uuid(department_id),
        "parent_id": _uuid(parent_id),
        "grade_id": _uuid(grade_id),
        "branch_id": _uuid(branch_id),
    }


@router.post("/hr/positions", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.org_chart")
def create_position(
    request: Request,
    db: Db,
    actor: CurrentActor,
    title: FormStr = "",
    department_id: FormStr = "",
    parent_id: FormStr = "",
    grade_id: FormStr = "",
    branch_id: FormStr = "",
) -> Response:
    values = {
        "title": title,
        "department_id": department_id,
        "parent_id": parent_id,
        "grade_id": grade_id,
        "branch_id": branch_id,
    }
    try:
        new_id = org.save_position(db, actor, None, **_position_fields(**values))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _chart_context(
            db, actor, position_errors=_errors(exc), position_values=values, position_open=True
        )
        return _render(request, "hr/org_chart.html", context, 422)
    return RedirectResponse(f"/hr/positions/{new_id}?notice=created", status_code=303)


# ------------------------------------------------------------------ one position


def _position_context(
    db: Session, actor: Actor, position_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    detail = org.position_detail(db, position_id)
    ids = {h.employee_id for h in detail.history}
    ids |= {c.holder.employee_id for c in detail.children if c.holder}
    ids |= {e for _, e in detail.chain if e}
    if detail.node.holder:
        ids.add(detail.node.holder.employee_id)
    node = detail.node
    return {
        "shell": _shell(db, actor, "Org chart", ["Org chart", node.title]),
        "detail": detail,
        "names": hr.names(db, ids),
        "kind_labels": KIND_LABELS,
        "can_manage": _can(db, actor, "org.structure.manage"),
        "can_assign": _can(db, actor, "org.assignment.manage"),
        "people": hr.assignable_people(db),
        "today": clock.today(),
        "assign_errors": {},
        "assign_values": {},
        "page_title": node.title,
        **_position_form_context(db),
        "position_values": {
            "title": node.title,
            "department_id": str(node.department_id),
            "parent_id": str(node.parent_id or ""),
            "grade_id": str(detail.grade_id or ""),
            "branch_id": str(detail.branch_id or ""),
        },
        **extra,
    }


NOTICES = {
    "created": "Position created. Assign someone to it when you're ready.",
    "saved": "Position saved.",
    "assigned": "Assignment saved. Reporting lines update straight away.",
    "ended": "Assignment ended.",
    "deactivated": "Position deactivated.",
    "activated": "Position reactivated.",
}


@router.get("/hr/positions/{position_id}", response_class=HTMLResponse)
@requires("org.structure.read")
@screen("hr.position")
def position_page(
    request: Request, db: Db, actor: CurrentActor, position_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    return _render(
        request,
        "hr/position.html",
        _position_context(db, actor, position_id, notice=NOTICES.get(notice)),
    )


@router.post("/hr/positions/{position_id}", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.position")
def update_position(
    request: Request,
    db: Db,
    actor: CurrentActor,
    position_id: uuid.UUID,
    title: FormStr = "",
    department_id: FormStr = "",
    parent_id: FormStr = "",
    grade_id: FormStr = "",
    branch_id: FormStr = "",
) -> Response:
    values = {
        "title": title,
        "department_id": department_id,
        "parent_id": parent_id,
        "grade_id": grade_id,
        "branch_id": branch_id,
    }
    try:
        org.save_position(db, actor, position_id, **_position_fields(**values))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _position_context(
            db, actor, position_id, position_errors=_errors(exc), position_open=True
        )
        context["position_values"] = values
        return _render(request, "hr/position.html", context, 422)
    return RedirectResponse(f"/hr/positions/{position_id}?notice=saved", status_code=303)


@router.post("/hr/positions/{position_id}/active", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.position")
def position_active(
    request: Request, db: Db, actor: CurrentActor, position_id: uuid.UUID, active: FormStr = "0"
) -> Response:
    try:
        org.set_position_active(db, actor, position_id, active=active == "1")
    except Conflict as exc:
        return _render(
            request,
            "hr/position.html",
            _position_context(db, actor, position_id, problem=exc.message),
            409,
        )
    notice = "activated" if active == "1" else "deactivated"
    return RedirectResponse(f"/hr/positions/{position_id}?notice={notice}", status_code=303)


@router.post("/hr/positions/{position_id}/assign", response_class=HTMLResponse)
@requires("org.assignment.manage")
@screen("hr.position")
def assign_person(
    request: Request,
    db: Db,
    actor: CurrentActor,
    position_id: uuid.UUID,
    employee_id: FormStr = "",
    kind: FormStr = "primary",
    starts: FormStr = "",
    ends: FormStr = "",
    reason: FormStr = "",
) -> Response:
    values = {
        "employee_id": employee_id,
        "kind": kind,
        "starts": starts,
        "ends": ends,
        "reason": reason,
    }
    try:
        start, end = parse_day(starts), parse_day(ends)
        problems = {}
        if _uuid(employee_id) is None:
            problems["employee_id"] = "Choose a person."
        if start is None:
            problems["start_date"] = "Choose a start date."
        if ends and end is None:
            problems["end_date"] = "Enter a date."
        if problems:
            raise ValidationFailed(field_errors=problems)
        assert start is not None  # noqa: S101  (checked above)
        hr.assign_to_position(
            db,
            actor,
            position_id=position_id,
            employee_id=uuid.UUID(employee_id),
            kind=kind,
            start_date=start,
            end_date=end,
            reason=reason,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _position_context(
            db,
            actor,
            position_id,
            assign_errors=_errors(exc),
            assign_values=values,
            assign_open=True,
        )
        return _render(request, "hr/position.html", context, 422)
    return RedirectResponse(f"/hr/positions/{position_id}?notice=assigned", status_code=303)


@router.post("/hr/assignments/{assignment_id}/end", response_class=HTMLResponse)
@requires("org.assignment.manage")
@screen("hr.position")
def end_assignment(
    request: Request,
    db: Db,
    actor: CurrentActor,
    assignment_id: uuid.UUID,
    last_day: FormStr = "",
    reason: FormStr = "",
) -> Response:
    position_id = org.assignment_position(db, assignment_id)
    if position_id is None:
        raise NotFound()
    try:
        day = parse_day(last_day)
        if day is None:
            raise ValidationFailed(field_errors={"last_day": "Choose the last day."})
        org.end_assignment(db, actor, assignment_id, last_day=day, reason=reason)
    except (ValidationFailed, Conflict) as exc:
        problem = " ".join(_errors(exc).values())
        return _render(
            request,
            "hr/position.html",
            _position_context(db, actor, position_id, problem=problem),
            422,
        )
    return RedirectResponse(f"/hr/positions/{position_id}?notice=ended", status_code=303)


# ------------------------------------------------------------------ departments


def _departments_context(
    db: Session, actor: Actor, *, inactive: bool = False, edit: str = "", **extra: Any
) -> dict[str, Any]:
    rows = org.list_departments(db, include_inactive=True)
    editing = next((d for d in rows if str(d.id) == edit), None)
    values = extra.pop("values", None) or (
        {
            "code": editing.code,
            "name": editing.name,
            "parent_id": str(editing.parent_id or ""),
            "cost_centre_id": str(editing.cost_centre_id or ""),
        }
        if editing
        else {}
    )
    return {
        "shell": _shell(db, actor, "Departments", ["Departments"]),
        "rows": [d for d in rows if d.is_active or inactive],
        "show_inactive": inactive,
        "inactive_count": sum(1 for d in rows if not d.is_active),
        "editing": editing,
        "values": values,
        "errors": {},
        "parent_choices": [d for d in rows if d.is_active and (not editing or d.id != editing.id)],
        "cost_centres": [c for c in org.list_reference(db, "cost_centres") if c.is_active],
        "can_manage": _can(db, actor, "org.structure.manage"),
        "drawer_open": editing is not None,
        "page_title": "Departments",
        **extra,
    }


@router.get("/hr/departments", response_class=HTMLResponse)
@requires("org.structure.read")
@screen("hr.departments")
def departments(
    request: Request,
    db: Db,
    actor: CurrentActor,
    inactive: int = 0,
    edit: str = "",
    notice: str = "",
) -> HTMLResponse:
    messages = {
        "saved": "Department saved.",
        "deactivated": "Department deactivated.",
        "activated": "Department reactivated.",
    }
    context = _departments_context(
        db, actor, inactive=bool(inactive), edit=edit, notice=messages.get(notice)
    )
    return _render(request, "hr/departments.html", context)


@router.post("/hr/departments", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.departments")
def save_department(
    request: Request,
    db: Db,
    actor: CurrentActor,
    department_id: FormStr = "",
    code: FormStr = "",
    name: FormStr = "",
    parent_id: FormStr = "",
    cost_centre_id: FormStr = "",
) -> Response:
    values = {"code": code, "name": name, "parent_id": parent_id, "cost_centre_id": cost_centre_id}
    try:
        org.save_department(
            db,
            actor,
            _uuid(department_id),
            code=code,
            name=name,
            parent_id=_uuid(parent_id),
            cost_centre_id=_uuid(cost_centre_id),
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _departments_context(
            db, actor, edit=department_id, values=values, errors=_errors(exc), drawer_open=True
        )
        return _render(request, "hr/departments.html", context, 422)
    return RedirectResponse("/hr/departments?notice=saved", status_code=303)


@router.post("/hr/departments/{department_id}/active", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.departments")
def department_active(
    request: Request, db: Db, actor: CurrentActor, department_id: uuid.UUID, active: FormStr = "0"
) -> Response:
    try:
        org.set_department_active(db, actor, department_id, active=active == "1")
    except Conflict as exc:
        return _render(
            request,
            "hr/departments.html",
            _departments_context(db, actor, inactive=True, problem=exc.message),
            409,
        )
    notice = "activated" if active == "1" else "deactivated"
    return RedirectResponse(f"/hr/departments?inactive=1&notice={notice}", status_code=303)


# ------------------------------------------------------------------ structure setup


def _structure_context(
    db: Session, actor: Actor, tab: str, *, edit: str = "", **extra: Any
) -> dict[str, Any]:
    tab = tab if tab in REFERENCE_TABS else "branches"
    items = org.list_reference(db, tab)
    editing = next((i for i in items if str(i.id) == edit), None)
    values = extra.pop("values", None) or (
        {
            "code": editing.code,
            "name": editing.name,
            "rank": editing.rank,
            "address": editing.address,
            "state": editing.state,
        }
        if editing
        else {}
    )
    return {
        "shell": _shell(db, actor, "Structure setup", ["Structure setup"]),
        "tab": tab,
        "tabs": [
            (k, label, len(org.list_reference(db, k)) if k != tab else len(items))
            for k, (label, _) in REFERENCE_TABS.items()
        ],
        "noun": REFERENCE_TABS[tab][1],
        "items": items,
        "editing": editing,
        "values": values,
        "errors": {},
        "can_manage": _can(db, actor, "org.structure.manage"),
        "drawer_open": editing is not None,
        "page_title": "Structure setup",
        **extra,
    }


@router.get("/hr/structure", response_class=HTMLResponse)
@requires("org.structure.read")
@screen("hr.structure")
def structure(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "branches",
    edit: str = "",
    notice: str = "",
) -> HTMLResponse:
    context = _structure_context(
        db, actor, tab, edit=edit, notice="Saved." if notice == "saved" else None
    )
    return _render(request, "hr/structure.html", context)


@router.post("/hr/structure/{tab}", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.structure")
def save_reference(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str,
    item_id: FormStr = "",
    code: FormStr = "",
    name: FormStr = "",
    rank: FormStr = "",
    address: FormStr = "",
    state: FormStr = "",
) -> Response:
    if tab not in REFERENCE_TABS:
        raise NotFound()
    values = {"code": code, "name": name, "rank": rank, "address": address, "state": state}
    try:
        org.save_reference(
            db,
            actor,
            tab,
            _uuid(item_id),
            code=code,
            name=name,
            rank=int(rank) if rank.strip().isdigit() else None,
            address=address,
            state=state,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _structure_context(
            db, actor, tab, edit=item_id, values=values, errors=_errors(exc), drawer_open=True
        )
        return _render(request, "hr/structure.html", context, 422)
    return RedirectResponse(f"/hr/structure?tab={tab}&notice=saved", status_code=303)


@router.post("/hr/structure/{tab}/{item_id}/active", response_class=HTMLResponse)
@requires("org.structure.manage")
@screen("hr.structure")
def reference_active(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str,
    item_id: uuid.UUID,
    active: FormStr = "0",
) -> Response:
    if tab not in REFERENCE_TABS:
        raise NotFound()
    try:
        org.set_reference_active(db, actor, tab, item_id, active=active == "1")
    except Conflict as exc:
        return _render(
            request,
            "hr/structure.html",
            _structure_context(db, actor, tab, problem=exc.message),
            409,
        )
    return RedirectResponse(f"/hr/structure?tab={tab}&notice=saved", status_code=303)


# ------------------------------------------------------------------ people


def _people_context(
    db: Session, actor: Actor, *, q: str = "", status: str = "", unplaced: bool = False
) -> dict[str, Any]:
    rows = hr.list_people(db, actor, query=q, status=status or None, without_position=unplaced)
    return {
        "shell": _shell(db, actor, "People", ["People"]),
        "rows": rows,
        "filters": {"q": q, "status": status, "unplaced": unplaced},
        "can_create": _can(db, actor, "hr.employee.create"),
        "kind_labels": KIND_LABELS,
        "page_title": "People",
    }


@router.get("/hr/people", response_class=HTMLResponse)
@requires("hr.employee.read")
@screen("hr.people")
def people(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    status: str = "",
    unplaced: int = 0,
) -> HTMLResponse:
    context = _people_context(db, actor, q=q, status=status, unplaced=bool(unplaced))
    return _render(request, "hr/people.html", context)
