"""Web routes for roles, people's access and delegations. Services do the checking;
these routes only translate forms and render (docs/PERMISSIONS.md)."""

import uuid
from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import catalogue, engine
from app.authz import service as authz
from app.authz.declarations import requires, requires_login, screen
from app.authz.permissions import Permission, Scope
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.web.access import require_actor
from app.web.forms import all_fields, end_of_day, parse_day, start_of_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
Fields = Annotated[dict[str, str], Depends(all_fields)]
FormStr = Annotated[str, Form()]

MODULE_LABELS = {
    "identity": "Users and sign-in",
    "authz": "Access control",
    "audit": "Audit",
    "system": "Technical settings",
}
MODULE_ICONS = {
    "identity": "users",
    "authz": "key-round",
    "audit": "scroll-text",
    "system": "settings",
}
SCOPES = list(Scope)


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _grouped(perms: list[Permission]) -> list[tuple[str, str, str, list[Permission]]]:
    groups: dict[str, list[Permission]] = defaultdict(list)
    for p in perms:
        groups[p.module].append(p)
    return [
        (m, MODULE_LABELS.get(m, m.title()), MODULE_ICONS.get(m, "layers"), groups[m])
        for m in sorted(
            groups, key=lambda m: list(MODULE_LABELS).index(m) if m in MODULE_LABELS else 99
        )
    ]


def _problem(exc: Exception) -> str:
    return (
        exc.message if isinstance(exc, ValidationFailed | Conflict | PermissionDenied) else str(exc)
    )


# ------------------------------------------------------------------ roles


def _roles_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Roles",
            crumbs=["Administration", "Roles"],
        ),
        "roles": authz.list_roles(db),
        "can_manage": bool(engine.check(db, actor.user_id, "authz.role.manage")),
        "create_errors": {},
        "create_values": {},
        "page_title": "Roles",
        **extra,
    }


@router.get("/admin/roles", response_class=HTMLResponse)
@requires("authz.role.read")
@screen("admin.roles")
def roles_page(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "authz/roles.html", _roles_context(db, actor))


@router.post("/admin/roles", response_class=HTMLResponse)
@requires("authz.role.manage")
@screen("admin.roles")
def create_role(
    request: Request,
    db: Db,
    actor: CurrentActor,
    name: FormStr = "",
    description: FormStr = "",
    copy_from: FormStr = "",
) -> Response:
    values = {"name": name, "description": description, "copy_from": copy_from}
    try:
        role_id = authz.create_role(
            db,
            actor,
            name=name,
            description=description,
            copy_from=uuid.UUID(copy_from) if copy_from else None,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", None) or {"form": _problem(exc)}
        return _render(
            request,
            "authz/roles.html",
            _roles_context(db, actor, create_errors=errors, create_values=values, create_open=True),
            422,
        )
    return RedirectResponse(f"/admin/roles/{role_id}?created=1", status_code=303)


def _editor_context(db: Session, actor: Actor, role_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    detail = authz.get_role(db, role_id)
    held = engine.load_grants(db, actor.user_id, include_delegations=False)
    can_manage = bool(engine.check(db, actor.user_id, "authz.role.manage"))
    editable = can_manage and not detail.role.protected
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Roles",
            crumbs=["Administration", "Roles", detail.role.name],
        ),
        "detail": detail,
        "groups": _grouped(list(catalogue.permissions().values())),
        "scopes": SCOPES,
        "grants": extra.pop("grants", None) or {k: s.value for k, s in detail.grants.items()},
        "held": {k: held.widest(k) for k in catalogue.permissions()},
        "editable": editable,
        "can_delete": editable and detail.role.template_key is None,
        "errors": {},
        "page_title": detail.role.name,
        **extra,
    }


@router.get("/admin/roles/{role_id}", response_class=HTMLResponse)
@requires("authz.role.read")
@screen("admin.role_editor")
def role_editor(
    request: Request, db: Db, actor: CurrentActor, role_id: uuid.UUID, created: int = 0
) -> HTMLResponse:
    notice = "Role created. Choose its permissions, then save." if created else None
    return _render(
        request, "authz/role_editor.html", _editor_context(db, actor, role_id, notice=notice)
    )


@router.post("/admin/roles/{role_id}", response_class=HTMLResponse)
@requires("authz.role.manage")
@screen("admin.role_editor")
def save_role(
    request: Request, db: Db, actor: CurrentActor, role_id: uuid.UUID, fields: Fields
) -> HTMLResponse:
    grants: dict[str, Scope] = {}
    for key in catalogue.permissions():
        value = fields.get(f"scope:{key}", "none")
        if value != "none":
            try:
                grants[key] = Scope(value)
            except ValueError:
                continue
    try:
        authz.update_role(
            db,
            actor,
            role_id,
            name=fields.get("name", ""),
            description=fields.get("description", ""),
            requires_mfa=fields.get("requires_mfa") == "on",
            grants=grants,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", {}) or {}
        context = _editor_context(
            db,
            actor,
            role_id,
            grants={k: s.value for k, s in grants.items()},
            errors=errors,
            problem=_problem(exc),
        )
        return _render(request, "authz/role_editor.html", context, 422)
    return _render(
        request,
        "authz/role_editor.html",
        _editor_context(db, actor, role_id, notice="Role saved. Changes apply straight away."),
    )


@router.post("/admin/roles/{role_id}/delete", response_class=HTMLResponse)
@requires("authz.role.manage")
@screen("admin.role_editor")
def delete_role(request: Request, db: Db, actor: CurrentActor, role_id: uuid.UUID) -> Response:
    try:
        authz.delete_role(db, actor, role_id)
    except (Conflict, PermissionDenied) as exc:
        return _render(
            request,
            "authz/role_editor.html",
            _editor_context(db, actor, role_id, problem=_problem(exc)),
            409,
        )
    return RedirectResponse("/admin/roles", status_code=303)


# ------------------------------------------------------------------ a person's access


def _access_context(db: Session, actor: Actor, user_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    access = authz.user_access(db, actor, user_id)
    effective = _grouped([catalogue.get(p.key) for p in access.effective])
    by_key = {p.key: p for p in access.effective}
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Users",
            crumbs=["Administration", "Users", access.full_name],
        ),
        "access": access,
        "effective_groups": [
            (m, label, icon, [by_key[p.key] for p in perms]) for m, label, icon, perms in effective
        ],
        "can_assign": bool(engine.check(db, actor.user_id, "authz.assignment.manage"))
        and user_id != actor.user_id,
        "is_self": user_id == actor.user_id,
        "today": clock.now().date().isoformat(),
        "assign_errors": {},
        "assign_values": {},
        "page_title": f"{access.full_name}: access",
        **extra,
    }


@router.get("/admin/users/{user_id}/access", response_class=HTMLResponse)
@requires("identity.user.read")
@screen("admin.user_access")
def user_access(request: Request, db: Db, actor: CurrentActor, user_id: uuid.UUID) -> HTMLResponse:
    return _render(request, "authz/user_access.html", _access_context(db, actor, user_id))


@router.post("/admin/users/{user_id}/access", response_class=HTMLResponse)
@requires("authz.assignment.manage")
@screen("admin.user_access")
def assign_role(
    request: Request,
    db: Db,
    actor: CurrentActor,
    user_id: uuid.UUID,
    role_id: FormStr = "",
    starts: FormStr = "",
    ends: FormStr = "",
    reason: FormStr = "",
) -> HTMLResponse:
    values = {"role_id": role_id, "starts": starts, "ends": ends, "reason": reason}
    start_day, end_day = parse_day(starts), parse_day(ends)
    try:
        if not role_id:
            raise ValidationFailed(field_errors={"role_id": "Choose a role."})
        if ends and end_day is None:
            raise ValidationFailed(field_errors={"valid_to": "Enter a date."})
        today = clock.now().date()
        authz.assign_role(
            db,
            actor,
            user_id=user_id,
            role_id=uuid.UUID(role_id),
            valid_from=start_of_day(start_day) if start_day and start_day > today else None,
            valid_to=end_of_day(end_day) if end_day else None,
            reason=reason,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", None) or {"form": _problem(exc)}
        context = _access_context(
            db, actor, user_id, assign_errors=errors, assign_values=values, assign_open=True
        )
        return _render(request, "authz/user_access.html", context, 422)
    return _render(
        request,
        "authz/user_access.html",
        _access_context(db, actor, user_id, notice="Role given. It applies straight away."),
    )


@router.post("/admin/assignments/{assignment_id}/end", response_class=HTMLResponse)
@requires("authz.assignment.manage")
@screen("admin.user_access")
def end_assignment(
    request: Request,
    db: Db,
    actor: CurrentActor,
    assignment_id: uuid.UUID,
    reason: FormStr = "",
) -> HTMLResponse:
    user_id = authz.assignment_owner(db, assignment_id)
    if user_id is None:
        raise NotFound()
    try:
        authz.end_assignment(db, actor, assignment_id, reason)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        return _render(
            request,
            "authz/user_access.html",
            _access_context(db, actor, user_id, problem=_problem(exc)),
            409,
        )
    return _render(
        request,
        "authz/user_access.html",
        _access_context(db, actor, user_id, notice="Role removed."),
    )


# ------------------------------------------------------------------ delegations


def _colleagues(db: Session, exclude: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    return list(
        db.execute(
            select(User.id, User.full_name, User.email)
            .where(User.status == "active", User.id != exclude)
            .order_by(User.full_name)
        ).all()
    )


def _delegations_context(
    db: Session, actor: Actor, *, everyone: bool, **extra: Any
) -> dict[str, Any]:
    show_past = bool(extra.pop("show_past", False))
    rows = authz.list_delegations(db, actor, everyone=everyone, include_past=show_past)
    if show_past:
        rows = [d for d in rows if d.status in {"ended", "revoked"}]
    crumbs = ["Administration", "Delegations"] if everyone else ["My account", "Delegations"]
    lendable = authz.delegable_permissions(db, actor.user_id)
    now = clock.now()
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin" if everyone else "home",
            active_section="Delegations" if everyone else None,
            crumbs=crumbs,
        ),
        "everyone": everyone,
        "current_user_id": actor.user_id,
        "show_past": show_past,
        "lending": [d for d in rows if d.from_user_id == actor.user_id] if not everyone else rows,
        "borrowing": [d for d in rows if d.to_user_id == actor.user_id] if not everyone else [],
        "lendable_groups": _grouped([catalogue.get(p.key) for p in lendable]),
        "lendable_scope": {p.key: p.scope for p in lendable},
        "colleagues": _colleagues(db, actor.user_id),
        "scopes": SCOPES,
        "max_days": authz.delegation_max_days(db),
        "today": now.date().isoformat(),
        "now": now,
        "base": "/admin/delegations" if everyone else "/delegations",
        "errors": {},
        "values": {},
        "page_title": "Delegations",
        **extra,
    }


def _create_delegation(
    db: Session, actor: Actor, fields: dict[str, str], *, everyone: bool
) -> list[uuid.UUID]:
    start_day, end_day = parse_day(fields.get("starts")), parse_day(fields.get("ends"))
    errors: dict[str, str] = {}
    if not fields.get("to_user_id"):
        errors["to_user_id"] = "Choose a colleague."
    if end_day is None:
        errors["ends_at"] = "Choose when it ends."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert end_day is not None  # noqa: S101  (checked above; narrows the type)
    keys = [
        k.removeprefix("perm:") for k, v in fields.items() if k.startswith("perm:") and v == "on"
    ]
    from_user = fields.get("from_user_id") if everyone else None
    try:
        scope = Scope(fields.get("scope", "self"))
    except ValueError as exc:
        raise ValidationFailed(field_errors={"scope": "Choose a scope."}) from exc
    today = clock.now().date()
    return authz.create_delegation(
        db,
        actor,
        to_user_id=uuid.UUID(fields["to_user_id"]),
        permission_keys=keys,
        scope=scope,
        starts_at=start_of_day(start_day) if start_day and start_day > today else None,
        ends_at=end_of_day(end_day),
        reason=fields.get("reason", ""),
        from_user_id=uuid.UUID(from_user) if from_user else None,
    )


def _delegations_post(
    request: Request, db: Session, actor: Actor, fields: dict[str, str], *, everyone: bool
) -> HTMLResponse:
    try:
        ids = _create_delegation(db, actor, fields, everyone=everyone)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", None) or {"form": _problem(exc)}
        context = _delegations_context(
            db, actor, everyone=everyone, errors=errors, values=fields, drawer_open=True
        )
        return _render(request, "authz/delegations.html", context, 422)
    noun = "permission" if len(ids) == 1 else "permissions"
    return _render(
        request,
        "authz/delegations.html",
        _delegations_context(
            db, actor, everyone=everyone, notice=f"Delegation created for {len(ids)} {noun}."
        ),
    )


@router.get("/delegations", response_class=HTMLResponse)
@requires_login
@screen("delegations")
def my_delegations(request: Request, db: Db, actor: CurrentActor, past: int = 0) -> HTMLResponse:
    return _render(
        request,
        "authz/delegations.html",
        _delegations_context(db, actor, everyone=False, show_past=bool(past)),
    )


@router.post("/delegations", response_class=HTMLResponse)
@requires_login
@screen("delegations")
def create_my_delegation(
    request: Request, db: Db, actor: CurrentActor, fields: Fields
) -> HTMLResponse:
    return _delegations_post(request, db, actor, fields, everyone=False)


@router.get("/admin/delegations", response_class=HTMLResponse)
@requires("authz.delegation.manage")
@screen("admin.delegations")
def all_delegations(request: Request, db: Db, actor: CurrentActor, past: int = 0) -> HTMLResponse:
    return _render(
        request,
        "authz/delegations.html",
        _delegations_context(db, actor, everyone=True, show_past=bool(past)),
    )


@router.post("/admin/delegations", response_class=HTMLResponse)
@requires("authz.delegation.manage")
@screen("admin.delegations")
def create_delegation_for_someone(
    request: Request, db: Db, actor: CurrentActor, fields: Fields
) -> HTMLResponse:
    return _delegations_post(request, db, actor, fields, everyone=True)


@router.post("/delegations/{delegation_id}/revoke", response_class=HTMLResponse)
@requires_login
@screen("delegations")
def revoke_delegation(
    request: Request,
    db: Db,
    actor: CurrentActor,
    delegation_id: uuid.UUID,
    back: FormStr = "/delegations",
) -> Response:
    everyone = back == "/admin/delegations" and bool(
        engine.check(db, actor.user_id, "authz.delegation.manage")
    )
    try:
        authz.revoke_delegation(db, actor, delegation_id)
    except Conflict as exc:
        return _render(
            request,
            "authz/delegations.html",
            _delegations_context(db, actor, everyone=everyone, problem=exc.message),
            409,
        )
    return _render(
        request,
        "authz/delegations.html",
        _delegations_context(db, actor, everyone=everyone, notice="Delegation revoked."),
    )
