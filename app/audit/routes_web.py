"""The audit log viewer (read-only)."""

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.audit import queries
from app.authz.declarations import requires, screen
from app.core.db import get_db
from app.identity.schemas import Actor
from app.web.access import require_actor
from app.web.forms import end_of_day, parse_day, start_of_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@router.get("/admin/audit", response_class=HTMLResponse)
@requires("audit.log.read")
@screen("admin.audit")
def audit_log(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    outcome: str = "",
    person: str = "",
    action: str = "",
    since: str = "",
    until: str = "",
    page: int = 1,
    event: str = "",
) -> HTMLResponse:
    since_day, until_day = parse_day(since), parse_day(until)
    filters = queries.AuditFilters(
        q=q,
        outcome=outcome,
        actor_id=_uuid(person),
        action_prefix=f"{action}." if action else "",
        since=start_of_day(since_day) if since_day else None,
        until=end_of_day(until_day) if until_day else None,
        page=page,
    )
    result = queries.search(db, filters)
    selected_id = _uuid(event)
    selected = queries.get(db, selected_id) if selected_id else None
    params = {
        k: v
        for k, v in {
            "q": q,
            "outcome": outcome,
            "person": person,
            "action": action,
            "since": since,
            "until": until,
        }.items()
        if v
    }
    return templates.TemplateResponse(
        request,
        "audit/log.html",
        {
            "shell": build_shell(
                db,
                actor,
                active_module="admin",
                active_section="Audit log",
                crumbs=["Administration", "Audit log"],
            ),
            "result": result,
            "selected": selected,
            "filters": {
                "q": q,
                "outcome": outcome,
                "person": person,
                "action": action,
                "since": since,
                "until": until,
            },
            "filtered": bool(params),
            "query": urlencode(params),
            "page_query": urlencode({**params, "page": result.page}),
            "people": queries.actors(db),
            "modules": queries.action_modules(db),
            "page_title": "Audit log",
        },
    )
