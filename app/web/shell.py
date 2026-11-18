"""Builds the app-shell context (sidebar, top bar) for a signed-in person."""

from sqlalchemy.orm import Session

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
    # Modules appear here as each phase delivers them; Phase 3 filters by permission.
    modules: list[NavModule] = [m for m in MODULES if m.key == "home"]
    if actor.is_super_admin:
        modules.append(ADMIN_MODULE)
    return ShellContext(
        org_name=organisation.display_name if organisation else "Kxmeleon",
        user=ShellUser(actor.full_name, "Super admin" if actor.is_super_admin else "Employee"),
        modules=tuple(modules),
        active_module=active_module,
        active_section=active_section,
        breadcrumbs=tuple(Breadcrumb(c) for c in crumbs),
        csrf_token=actor.csrf_token,
    )
