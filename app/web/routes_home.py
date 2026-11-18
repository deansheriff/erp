"""The signed-in home page. Module dashboards are added to it as phases deliver them."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires_login, screen
from app.core.db import get_db
from app.identity.schemas import Actor
from app.mail import service as mail
from app.web.access import require_actor
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
@requires_login
@screen("core.home")
def home(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[Actor, Depends(require_actor)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "shell": build_shell(db, actor, active_module="home", crumbs=["Home"]),
            "first_name": actor.full_name.split()[0],
            "is_admin": bool(engine.check(db, actor.user_id, "system.mail.manage")),
            "mail_ready": mail.is_configured(db),
            "mfa_enabled": actor.mfa_enabled,
            "page_title": "Home",
        },
    )
