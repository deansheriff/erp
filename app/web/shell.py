"""Builds the app-shell context (sidebar, top bar) for a signed-in person."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.authz import engine
from app.identity.schemas import Actor
from app.org import service as org
from app.web.navigation import (
    ADMIN_MODULE,
    MODULES,
    Breadcrumb,
    NavModule,
    ShellContext,
    ShellUser,
)


def build_shell(
    db: Session,
    actor: Actor,
    *,
    active_module: str,
    crumbs: list[str],
    active_section: str | None = None,
) -> ShellContext:
    organisation = org.get_organization(db)
    grants = engine.load_grants(db, actor.user_id)
    # Modules appear here as each phase delivers them, and only with a permission in them.
    modules: list[NavModule] = [m for m in MODULES if m.key == "home"]
    admin_sections = tuple(
        s for s in ADMIN_MODULE.sections if s.permission is None or grants.holds(s.permission)
    )
    if admin_sections:
        modules.append(replace(ADMIN_MODULE, href=admin_sections[0].href, sections=admin_sections))
    return ShellContext(
        org_name=organisation.display_name if organisation else "Kxmeleon",
        user=ShellUser(actor.full_name, ", ".join(grants.role_names[:2]) or "No role yet"),
        modules=tuple(modules),
        active_module=active_module,
        active_section=active_section,
        breadcrumbs=tuple(Breadcrumb(c) for c in crumbs),
        csrf_token=actor.csrf_token,
    )
