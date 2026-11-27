"""Support web routes (ADR 0032): tickets and their conversation, raising one, the SLA
report and settings. Routes translate forms and render; the services do the work."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.support import report, setup, tickets
from app.modules.support.files import SUPPORT_ATTACHMENT
from app.modules.support.models import PRIORITIES
from app.modules.support.tickets import TicketInput
from app.web.access import require_actor
from app.web.forms import all_fields
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
Fields = Annotated[dict[str, str], Depends(all_fields)]
Upload = Annotated[ReceivedUpload, Depends(upload_body(SUPPORT_ATTACHMENT.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
TAB_LABELS = {
    "mine": "My tickets",
    "queue": "Team queue",
    "assigned": "Assigned to me",
    "all": "All",
}
NOTICES = {
    "raised": "Ticket raised. The team will pick it up.",
    "taken": "It's yours.",
    "assigned": "Assigned.",
    "replied": "Sent.",
    "noted": "Note added.",
    "waiting": "Sent, and waiting on the requester.",
    "resumed": "Back in progress.",
    "resolved": "Resolved. The requester has been told.",
    "reopened": "Reopened.",
    "rated": "Thanks for rating it.",
    "priority": "Priority changed.",
    "attached": "File attached. It's checked for viruses before anyone can open it.",
    "saved": "Saved.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="support", active_section=section, crumbs=["Support", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _common(db: Session) -> dict[str, Any]:
    return {
        "priority_labels": setup.PRIORITY_LABELS,
        "priority_tones": setup.PRIORITY_TONES,
        "status_labels": setup.STATUS_LABELS,
        "status_tones": setup.STATUS_TONES,
        "per_day": setup.day_minutes(db),
    }


# ------------------------------------------------------------------ tickets


@router.get("/support", response_class=HTMLResponse)
@requires_login
@screen("support.tickets")
def ticket_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "",
    team: str = "",
    status: str = "",
) -> HTMLResponse:
    agent = setup.can(db, actor, setup.WORK)
    tabs = [(k, v) for k, v in TAB_LABELS.items() if agent or k == "mine"]
    tab = tab if tab in dict(tabs) else ("queue" if agent else "mine")
    context = {
        "shell": _shell(db, actor, "Tickets"),
        "tab": tab,
        "tabs": tabs,
        "rows": tickets.list_tickets(db, actor, tab, team=team, status=status),
        "counts": tickets.counts(db, actor),
        "teams": setup.teams(db) if agent else [],
        "team": team,
        "status": status,
        "can_raise": setup.can(db, actor, setup.CREATE) or agent,
        "page_title": "Support tickets",
        **_common(db),
    }
    return _render(request, "support/tickets.html", context)


def _new_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    agent = setup.can(db, actor, setup.WORK)
    teams = {t.id: t.name for t in setup.teams(db)}
    targets = setup.targets(db)
    return {
        "shell": _shell(db, actor, "Tickets"),
        "categories": [
            (c.id, f"{teams.get(c.team_id, '')} · {c.name}")
            for c in setup.categories(db, active_only=True)
        ],
        "priorities": PRIORITIES,
        "targets": targets,
        "is_agent": agent,
        "on_staff": people.employee_of_user(db, actor.user_id) is not None,
        "customers": finance.customer_choices(db) if agent else [],
        "values": {"priority": "normal"},
        "errors": {},
        "page_title": "New ticket",
        **_common(db),
        **extra,
    }


@router.get("/support/new", response_class=HTMLResponse)
@requires_login
@screen("support.ticket_new")
def new_ticket(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "support/new.html", _new_context(db, actor))


@router.post("/support/new", response_class=HTMLResponse)
@requires_login
@screen("support.ticket_new")
def raise_ticket(request: Request, db: Db, actor: CurrentActor, fields: Fields) -> Response:
    for_customer = bool(fields.get("customer_id"))
    data = TicketInput(
        fields.get("category_id", ""),
        fields.get("priority", ""),
        fields.get("subject", ""),
        fields.get("description", ""),
        fields.get("customer_id", "") if for_customer else "",
        fields.get("contact_email", "") if for_customer else "",
    )
    try:
        with db.begin_nested():
            ticket = tickets.raise_ticket(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _new_context(db, actor, values=fields, errors=_errors(exc))
        return _render(request, "support/new.html", context, 422)
    return _see_other(f"/support/{ticket}?notice=raised")


def _ticket_context(
    db: Session, actor: Actor, ticket_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    view = tickets.get(db, actor, ticket_id)
    return {
        "shell": _shell(db, actor, "Tickets"),
        "view": view,
        "t": view.row.ticket,
        "priorities": PRIORITIES,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{view.row.ticket.number} {view.row.ticket.subject}",
        **_common(db),
        **extra,
    }


@router.get("/support/{ticket_id:uuid}", response_class=HTMLResponse)
@requires_login
@screen("support.ticket")
def ticket_page(
    request: Request, db: Db, actor: CurrentActor, ticket_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _ticket_context(db, actor, ticket_id, notice=NOTICES.get(notice))
    return _render(request, "support/ticket.html", context)


def _reply_notice(f: dict[str, str]) -> str:
    if f.get("kind") == "note":
        return "noted"
    return "waiting" if f.get("then") == "waiting" else "replied"


def _run(db: Session, actor: Actor, ticket_id: uuid.UUID, action: str, f: dict[str, str]) -> str:
    """One ticket action; returns the notice key."""
    runs: dict[str, tuple[Any, str]] = {
        "take": (lambda: tickets.take(db, actor, ticket_id), "taken"),
        "assign": (
            lambda: tickets.assign(db, actor, ticket_id, f.get("assignee_id", "")),
            "assigned",
        ),
        "reply": (
            lambda: tickets.reply(
                db,
                actor,
                ticket_id,
                body=f.get("body", ""),
                internal=f.get("kind") == "note",
                then=f.get("then", ""),
            ),
            _reply_notice(f),
        ),
        "resume": (lambda: tickets.resume(db, actor, ticket_id), "resumed"),
        "resolve": (
            lambda: tickets.resolve(db, actor, ticket_id, resolution=f.get("resolution", "")),
            "resolved",
        ),
        "reopen": (lambda: tickets.reopen(db, actor, ticket_id), "reopened"),
        "rate": (
            lambda: tickets.rate(
                db, actor, ticket_id, score=f.get("score", ""), comment=f.get("comment", "")
            ),
            "rated",
        ),
        "priority": (
            lambda: tickets.set_priority(db, actor, ticket_id, f.get("priority", "")),
            "priority",
        ),
    }
    if action not in runs:
        raise NotFound()
    run, notice = runs[action]
    run()
    return notice


@router.post("/support/{ticket_id:uuid}/attach", response_class=HTMLResponse)
@requires_login
@screen("support.ticket")
def attach_file(db: Db, actor: CurrentActor, ticket_id: uuid.UUID, upload: Upload) -> Response:
    if upload.errors:
        raise ValidationFailed(field_errors=upload.errors)
    tickets.attach(
        db, actor, ticket_id, filename=upload.filename, mime=upload.content_type, data=upload.data
    )
    files.request_scan()
    return _see_other(f"/support/{ticket_id}?notice=attached")


@router.post("/support/{ticket_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("support.ticket")
def ticket_action(
    request: Request, db: Db, actor: CurrentActor, ticket_id: uuid.UUID, action: str, f: Fields
) -> Response:
    try:
        with db.begin_nested():
            notice = _run(db, actor, ticket_id, action, f)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _ticket_context(db, actor, ticket_id, values=f, errors=_errors(exc))
        return _render(request, "support/ticket.html", context, 422)
    return _see_other(f"/support/{ticket_id}?notice={notice}")


# ------------------------------------------------------------------ report and settings


@router.get("/support/report", response_class=HTMLResponse)
@requires("support.report.read")
@screen("support.sla_report")
def sla_report(
    request: Request, db: Db, actor: CurrentActor, month: str = "", team: str = ""
) -> HTMLResponse:
    try:
        chosen = date.fromisoformat(f"{month}-01") if month else clock.today().replace(day=1)
    except ValueError:
        chosen = clock.today().replace(day=1)
    context = {
        "shell": _shell(db, actor, "SLA report"),
        "report": report.build(db, actor, chosen, team=team),
        "month": chosen,
        "teams": setup.teams(db),
        "team": team,
        "page_title": "SLA report",
        **_common(db),
    }
    return _render(request, "support/report.html", context)


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    setup.require(db, actor, setup.MANAGE)
    teams = setup.teams(db)
    names = people.names(
        db,
        {a for t in teams for a in setup.agents_of(db, t.id)}
        | {t.lead_employee_id for t in teams if t.lead_employee_id},
    )
    return {
        "shell": _shell(db, actor, "Settings"),
        "teams": [
            (
                t,
                sorted(names.get(a, "") for a in setup.agents_of(db, t.id)),
                setup.agents_of(db, t.id),
            )
            for t in teams
        ],
        "names": names,
        "categories": setup.categories(db),
        "team_names": {t.id: t.name for t in teams},
        "settings": setup.settings(db),
        "targets": setup.targets(db),
        "priorities": PRIORITIES,
        "staff": [(e, n) for e, n, _ in people.assignable_people(db)],
        "editing": extra.pop("editing", ""),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Support settings",
        **_common(db),
        **extra,
    }


@router.get("/support/settings", response_class=HTMLResponse)
@requires("support.settings.manage")
@screen("support.settings")
def settings_page(
    request: Request, db: Db, actor: CurrentActor, team: str = "", notice: str = ""
) -> HTMLResponse:
    context = _settings_context(db, actor, editing=team, notice=NOTICES.get(notice))
    return _render(request, "support/settings.html", context)


@router.post("/support/settings/{what}", response_class=HTMLResponse)
@requires("support.settings.manage")
@screen("support.settings")
def save_settings(
    request: Request,
    db: Db,
    actor: CurrentActor,
    what: str,
    fields: Fields,
    agent: FormList = [],  # noqa: B006  (FastAPI form lists)
) -> Response:
    """team, category or hours."""
    try:
        with db.begin_nested():
            if what == "team":
                setup.save_team(
                    db,
                    actor,
                    setup.uuid_or_none(fields.get("team_id")),
                    setup.TeamInput(
                        fields.get("team_name", ""),
                        fields.get("lead_id", ""),
                        agent,
                        bool(fields.get("active")),
                    ),
                )
            elif what == "category":
                setup.save_category(
                    db,
                    actor,
                    setup.uuid_or_none(fields.get("category_id")),
                    name=fields.get("category_name", ""),
                    team_id=fields.get("team_id", ""),
                    active=bool(fields.get("active", "1")),
                )
            elif what == "hours":
                setup.save_settings(
                    db,
                    actor,
                    setup.SettingsInput(
                        fields.get("day_start", ""),
                        fields.get("day_end", ""),
                        fields.get("warn_pct", ""),
                        fields.get("close_after_days", ""),
                        {p: fields.get(f"response_{p}", "") for p in PRIORITIES},
                        {p: fields.get(f"resolve_{p}", "") for p in PRIORITIES},
                    ),
                )
            else:
                raise NotFound()
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(
            db, actor, values={**fields, "agent": agent}, errors=_errors(exc), what=what
        )
        return _render(request, "support/settings.html", context, 422)
    return _see_other("/support/settings?notice=saved")
