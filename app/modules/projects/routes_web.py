"""Projects web routes (ADR 0031): projects and their plan, timesheets and approvals,
profitability, cost rates and settings. Routes translate forms and render; the services
do the work."""

import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.projects import common, figures, projects, timesheets
from app.modules.projects.models import CATEGORIES
from app.modules.projects.projects import ProjectInput, TaskInput
from app.modules.projects.timesheets import Cell
from app.org import service as org
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
DomainProblem = ValidationFailed | Conflict | PermissionDenied
TABS = (("mine", "Mine"), ("active", "Active"), ("all", "All"))
BLANK_ROWS = 2
NOTICES = {
    "saved": "Saved.",
    "status": "Status changed.",
    "milestone": "Milestone saved.",
    "task": "Task saved.",
    "deleted": "Task deleted.",
    "week": "Hours saved.",
    "approved": "Approved.",
    "rejected": "Rejected. It's back with them to correct.",
    "rate": "Rate saved.",
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
        db, actor, active_module="projects", active_section=section, crumbs=["Projects", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ projects


@router.get("/projects", response_class=HTMLResponse)
@requires_login
@screen("projects.list")
def project_list(
    request: Request, db: Db, actor: CurrentActor, tab: str = "mine", notice: str = ""
) -> HTMLResponse:
    tab = tab if tab in dict(TABS) else "mine"
    context = {
        "shell": _shell(db, actor, "Projects"),
        "tab": tab,
        "tabs": TABS,
        "rows": projects.list_projects(db, actor, tab),
        "labels": common.STATUS_LABELS,
        "tones": common.STATUS_TONES,
        "can_create": common.can(db, actor, common.CREATE),
        "notice": NOTICES.get(notice),
        "page_title": "Projects",
    }
    return _render(request, "projects/list.html", context)


def _form_context(
    db: Session, actor: Actor, project_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = projects.get(db, actor, project_id) if project_id else None
    if view is not None and not view.can_manage:
        raise NotFound()
    return {
        "shell": _shell(db, actor, "Projects"),
        "view": view,
        "budget": projects.budget_of(db, project_id) if project_id else {},
        "customers": finance.customer_choices(db),
        "staff": projects.staff(db),
        "centres": org.cost_centre_choices(db),
        "categories": [(c, common.CATEGORY_LABELS[c]) for c in CATEGORIES],
        "me": common.me(db, actor),
        "values": {},
        "errors": {},
        "page_title": f"Edit {view.project.code}" if view else "New project",
        **extra,
    }


@router.get("/projects/new", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.project_form")
def new_project(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "projects/project_form.html", _form_context(db, actor, None))


@router.get("/projects/{project_id:uuid}/edit", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.project_form")
def edit_project(
    request: Request, db: Db, actor: CurrentActor, project_id: uuid.UUID
) -> HTMLResponse:
    return _render(request, "projects/project_form.html", _form_context(db, actor, project_id))


@router.post("/projects", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.project_form")
def save_project(
    request: Request,
    db: Db,
    actor: CurrentActor,
    fields: Fields,
    member: FormList = [],  # noqa: B006  (FastAPI form lists)
    milestone_name: FormList = [],  # noqa: B006
    milestone_due: FormList = [],  # noqa: B006
) -> Response:
    existing = common.uuid_or_none(fields.get("project_id"))
    data = ProjectInput(
        fields.get("name", ""),
        fields.get("customer_id", ""),
        fields.get("manager_id", ""),
        parse_day(fields.get("start_date", "")),
        parse_day(fields.get("end_date", "")),
        fields.get("cost_centre_id", ""),
        fields.get("description", ""),
        {c: fields.get(f"budget_{c}", "") for c in CATEGORIES},
        member,
        [
            (name, parse_day(milestone_due[i] if i < len(milestone_due) else ""))
            for i, name in enumerate(milestone_name)
            if name.strip()
        ],
    )
    try:
        with db.begin_nested():
            saved = projects.save(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values: dict[str, Any] = {**fields, "member": member}
        context = _form_context(db, actor, existing, values=values, errors=_errors(exc))
        return _render(request, "projects/project_form.html", context, 422)
    return _see_other(f"/projects/{saved}?notice=saved")


def _project_context(
    db: Session, actor: Actor, project_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    view = projects.get(db, actor, project_id)
    return {
        "shell": _shell(db, actor, "Projects"),
        "view": view,
        "labels": common.STATUS_LABELS,
        "tones": common.STATUS_TONES,
        "task_labels": projects.TASK_LABELS,
        "task_tones": projects.TASK_TONES,
        "categories": [(c, common.CATEGORY_LABELS[c]) for c in CATEGORIES],
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{view.project.code} {view.project.name}",
        **extra,
    }


@router.get("/projects/{project_id:uuid}", response_class=HTMLResponse)
@requires_login
@screen("projects.project")
def project_page(
    request: Request, db: Db, actor: CurrentActor, project_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _project_context(db, actor, project_id, notice=NOTICES.get(notice))
    return _render(request, "projects/project.html", context)


@router.post("/projects/milestones/{milestone_id:uuid}/toggle", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.project")
def toggle_milestone(db: Db, actor: CurrentActor, milestone_id: uuid.UUID) -> Response:
    project_id = projects.toggle_milestone(db, actor, milestone_id)
    return _see_other(f"/projects/{project_id}?notice=milestone")


# ------------------------------------------------------------------ tasks


def _task_context(
    db: Session, actor: Actor, view: projects.ProjectView, task: Any, **extra: Any
) -> dict[str, Any]:
    if not view.can_manage:
        raise NotFound()
    return {
        "shell": _shell(db, actor, "Projects"),
        "view": view,
        "task": task,
        "task_labels": projects.TASK_LABELS,
        "values": {},
        "errors": {},
        "page_title": task.title if task else "New task",
        **extra,
    }


@router.get("/projects/{project_id:uuid}/tasks/new", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.task")
def new_task(
    request: Request, db: Db, actor: CurrentActor, project_id: uuid.UUID, milestone: str = ""
) -> HTMLResponse:
    view = projects.get(db, actor, project_id)
    context = _task_context(db, actor, view, None, values={"milestone_id": milestone})
    return _render(request, "projects/task.html", context)


@router.get("/projects/tasks/{task_id:uuid}", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.task")
def task_page(request: Request, db: Db, actor: CurrentActor, task_id: uuid.UUID) -> HTMLResponse:
    view, task = projects.task_of(db, actor, task_id)
    return _render(request, "projects/task.html", _task_context(db, actor, view, task))


@router.post("/projects/{project_id:uuid}/tasks", response_class=HTMLResponse)
@requires("projects.project.create")
@screen("projects.task")
def save_task(
    request: Request, db: Db, actor: CurrentActor, project_id: uuid.UUID, fields: Fields
) -> Response:
    task_id = common.uuid_or_none(fields.get("task_id"))
    data = TaskInput(
        fields.get("title", ""),
        fields.get("milestone_id", ""),
        fields.get("assignee_id", ""),
        parse_day(fields.get("due_date", "")),
        fields.get("status", "todo"),
        fields.get("description", ""),
    )
    try:
        with db.begin_nested():
            projects.save_task(db, actor, project_id, task_id, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        view = projects.get(db, actor, project_id)
        task = projects.task_of(db, actor, task_id)[1] if task_id else None
        context = _task_context(db, actor, view, task, values=fields, errors=_errors(exc))
        return _render(request, "projects/task.html", context, 422)
    return _see_other(f"/projects/{project_id}?notice=task#plan")


@router.post("/projects/tasks/{task_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("projects.project")
def task_action(
    db: Db, actor: CurrentActor, task_id: uuid.UUID, action: str, status: FormStr = ""
) -> Response:
    """status (the assignee or manager) or delete (the manager)."""
    if action == "status":
        project_id = projects.set_task_status(db, actor, task_id, status)
        return _see_other(f"/projects/{project_id}?notice=task#plan")
    if action == "delete":
        project_id = projects.delete_task(db, actor, task_id)
        return _see_other(f"/projects/{project_id}?notice=deleted#plan")
    raise NotFound()


# ------------------------------------------------------------------ timesheets


def _week(value: str) -> date:
    return common.monday(parse_day(value) or clock.today())


def _timesheet_context(db: Session, actor: Actor, week: date, **extra: Any) -> dict[str, Any]:
    view = timesheets.week_view(db, actor, week)
    return {
        "shell": _shell(db, actor, "My timesheet"),
        "week": view,
        "previous": week - timedelta(days=7),
        "next": week + timedelta(days=7),
        "this_week": common.monday(clock.today()),
        "blank_rows": BLANK_ROWS,
        "labels": timesheets.STATUS_LABELS,
        "tones": timesheets.STATUS_TONES,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"Week of {week:%d %b %Y}",
        **extra,
    }


@router.get("/projects/timesheet", response_class=HTMLResponse)
@requires("projects.time.log")
@screen("projects.timesheet")
def timesheet_page(
    request: Request, db: Db, actor: CurrentActor, week: str = "", notice: str = ""
) -> HTMLResponse:
    message = NOTICES.get(notice) or (
        f"Sent to {notice[5:]}." if notice.startswith("sent:") else None
    )
    try:
        context = _timesheet_context(db, actor, _week(week), notice=message)
    except PermissionDenied:
        context = {
            "shell": _shell(db, actor, "My timesheet"),
            "no_staff": True,
            "page_title": "My timesheet",
        }
    return _render(request, "projects/timesheet.html", context)


def _cells(fields: dict[str, str], week: date) -> list[Cell]:
    out = []
    rows = sorted({k.split("_")[1] for k in fields if k.startswith("rp_")}, key=int)
    for r in rows:
        for d in range(7):
            out.append(
                Cell(
                    fields.get(f"rp_{r}", ""),
                    fields.get(f"rt_{r}", ""),
                    week + timedelta(days=d),
                    fields.get(f"h_{r}_{d}", ""),
                )
            )
    return out


@router.post("/projects/timesheet", response_class=HTMLResponse)
@requires("projects.time.log")
@screen("projects.timesheet")
def save_timesheet(request: Request, db: Db, actor: CurrentActor, fields: Fields) -> Response:
    week = _week(fields.get("week", ""))
    submit = common.uuid_or_none(fields.get("submit", ""))
    try:
        with db.begin_nested():
            if fields.get("save", "1") != "0":
                timesheets.save_week(db, actor, week, _cells(fields, week))
            sent_to = timesheets.submit(db, actor, submit, week) if submit else None
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _timesheet_context(db, actor, week, values=fields, errors=_errors(exc))
        return _render(request, "projects/timesheet.html", context, 422)
    notice = f"sent:{sent_to}" if sent_to else "week"
    return _see_other(f"/projects/timesheet?week={week.isoformat()}&notice={notice}")


@router.get("/projects/approvals", response_class=HTMLResponse)
@requires_login
@screen("projects.approvals")
def approvals_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = {
        "shell": _shell(db, actor, "Time approvals"),
        "rows": timesheets.pending(db, actor),
        "values": {},
        "errors": {},
        "notice": NOTICES.get(notice),
        "page_title": "Time approvals",
    }
    return _render(request, "projects/approvals.html", context)


@router.post("/projects/approvals/{sheet_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("projects.approvals")
def decide_sheet(
    request: Request,
    db: Db,
    actor: CurrentActor,
    sheet_id: uuid.UUID,
    action: str,
    note: FormStr = "",
) -> Response:
    if action not in ("approve", "reject"):
        raise NotFound()
    try:
        with db.begin_nested():
            timesheets.decide(db, actor, sheet_id, approve=action == "approve", note=note)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = {
            "shell": _shell(db, actor, "Time approvals"),
            "rows": timesheets.pending(db, actor),
            "values": {f"note_{sheet_id}": note},
            "errors": _errors(exc),
            "notice": None,
            "page_title": "Time approvals",
        }
        return _render(request, "projects/approvals.html", context, 422)
    return _see_other(f"/projects/approvals?notice={action}d")


# ------------------------------------------------------------------ figures and settings


@router.get("/projects/profitability", response_class=HTMLResponse)
@requires("projects.project.read")
@screen("projects.profitability")
def profitability_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    status: str = "active",
    start: str = "",
    end: str = "",
) -> HTMLResponse:
    rows = figures.profitability(
        db, actor, status=status, start=parse_day(start), end=parse_day(end)
    )
    context = {
        "shell": _shell(db, actor, "Profitability"),
        "rows": rows,
        "status": status,
        "start": start,
        "end": end,
        "labels": common.STATUS_LABELS,
        "totals": figures.Figures(
            revenue=sum((r.figures.revenue for r in rows), common.ZERO),
            actual={c: sum((r.figures.actual[c] for r in rows), common.ZERO) for c in CATEGORIES},
            budget={c: sum((r.figures.budget[c] for r in rows), common.ZERO) for c in CATEGORIES},
        ),
        "page_title": "Project profitability",
    }
    return _render(request, "projects/profitability.html", context)


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    can_rates = common.can(db, actor, common.RATES)
    can_settings = common.can(db, actor, common.SETTINGS)
    if not (can_rates or can_settings):
        raise PermissionDenied()
    return {
        "shell": _shell(db, actor, "Settings"),
        "settings": common.settings(db),
        "rates": figures.rate_rows(db, actor) if can_rates else [],
        "can_rates": can_rates,
        "can_settings": can_settings,
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Project settings",
        **extra,
    }


@router.get("/projects/settings", response_class=HTMLResponse)
@requires_login
@screen("projects.settings")
def settings_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _settings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "projects/settings.html", context)


@router.post("/projects/settings/{what}", response_class=HTMLResponse)
@requires_login
@screen("projects.settings")
def save_settings(
    request: Request, db: Db, actor: CurrentActor, what: str, fields: Fields
) -> Response:
    """alerts or rate."""
    try:
        with db.begin_nested():
            if what == "alerts":
                figures.save_settings(
                    db,
                    actor,
                    warn_at=fields.get("warn_at", ""),
                    alert_at=fields.get("alert_at", ""),
                )
                notice = "settings"
            elif what == "rate":
                figures.add_rate(
                    db,
                    actor,
                    employee_id=fields.get("employee_id", ""),
                    hourly=fields.get("hourly", ""),
                    effective_from=parse_day(fields.get("effective_from", "")),
                )
                notice = "rate"
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(db, actor, values=fields, errors=_errors(exc))
        return _render(request, "projects/settings.html", context, 422)
    return _see_other(f"/projects/settings?notice={notice}")


# Last, so the more specific routes above match first.
@router.post("/projects/{project_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("projects.project")
def project_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    project_id: uuid.UUID,
    action: str,
    status: FormStr = "",
    name: FormStr = "",
    due: FormStr = "",
) -> Response:
    """status or milestone: the services check who may."""
    try:
        with db.begin_nested():
            if action == "status":
                projects.move(db, actor, project_id, status)
                notice = "status"
            elif action == "milestone":
                projects.add_milestone(db, actor, project_id, name=name, due=parse_day(due))
                notice = "milestone"
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _project_context(
            db, actor, project_id, values={"milestone_name": name, "due": due}, errors=_errors(exc)
        )
        return _render(request, "projects/project.html", context, 422)
    return _see_other(f"/projects/{project_id}?notice={notice}")
