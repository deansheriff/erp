"""Builds the app-shell context (sidebar, top bar) for a signed-in person."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.permissions import Scope
from app.files import service as files
from app.identity.schemas import Actor
from app.org import service as org
from app.web.navigation import (
    ADMIN_MODULE,
    MODULES,
    Breadcrumb,
    NavModule,
    NavSection,
    ShellContext,
    ShellUser,
)


def _visible(section: NavSection, grants: engine.Grants) -> bool:
    if section.permission is None:
        return True
    needed = (section.permission,) if isinstance(section.permission, str) else section.permission
    if section.wide:
        return any(grants.widest(p) not in {None, Scope.SELF} for p in needed)
    return any(grants.holds(p) for p in needed)


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
    # A module appears once it has a section the viewer may use (Home always does).
    modules: list[NavModule] = []
    for module in (*MODULES, ADMIN_MODULE):
        if module.key == "home":
            modules.append(module)
            continue
        sections = tuple(s for s in module.sections if _visible(s, grants))
        if sections:
            modules.append(replace(module, href=sections[0].href, sections=sections))
    org_id = org.organization_id(db)
    logo = (
        files.current_file(db, purpose_key="organization_logo", entity_id=str(org_id))
        if org_id
        else None
    )
    return ShellContext(
        org_logo_url=f"/files/{logo.id}" if logo else None,
        org_name=organisation.display_name if organisation else "Kxmeleon",
        user=ShellUser(actor.full_name, ", ".join(grants.role_names[:2]) or "No role yet"),
        modules=tuple(modules),
        active_module=active_module,
        active_section=active_section,
        breadcrumbs=tuple(Breadcrumb(c) for c in crumbs),
        csrf_token=actor.csrf_token,
    )
