"""Managing roles, role assignments and delegations (ADR 0013, docs/PERMISSIONS.md).

Two rules stop anyone raising their own access:
- you can only grant (to a role, a person or a delegate) what you hold yourself, and no
  more widely than you hold it;
- you can't change your own roles (separation of duties rule `authz.no_self_assignment`).

Every change bumps the authz version, so cached grants are never stale, and is audited.
"""

import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.authz.models import Delegation, PermissionRow, Role, RoleAssignment, RoleGrant
from app.authz.permissions import Scope
from app.authz.role_templates import SUPER_ADMIN
from app.authz.schemas import (
    AssignmentView,
    DelegationView,
    EffectivePermission,
    RoleDetail,
    RoleSummary,
    UserAccess,
)
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import SecuritySettings, User
from app.identity.schemas import Actor

# Delegating these would hand over control of access itself or of technical settings.
# Flagged as a default for the product owner to confirm (Phase 3 report).
NOT_DELEGABLE_MODULES = frozenset({"authz", "system"})
MIN_REASON = 5
LAST_SUPER_ADMIN = (
    "This is the last active Super Administrator. Give the role to someone else first."
)


# ------------------------------------------------------------------ helpers


def _audit(db: Session, actor: Actor | None, action: str, **kwargs: Any) -> None:
    record_by(db, actor, action, **kwargs)


def _changed(db: Session) -> None:
    engine.bump_version(db)


def _reason(reason: str) -> str:
    reason = reason.strip()
    if len(reason) < MIN_REASON:
        raise ValidationFailed(field_errors={"reason": "Say why, in a few words."})
    return reason


def _own_grants(db: Session, actor: Actor) -> engine.Grants:
    """What the actor holds through their own roles. Delegated permissions can't be
    passed on."""
    return engine.load_grants(db, actor.user_id, include_delegations=False)


def require(db: Session, actor: Actor, permission: str) -> None:
    """For services: refuse unless the actor holds `permission` (at any scope)."""
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()


def _can_grant(held: engine.Grants, key: str, scope: Scope) -> bool:
    widest = held.widest(key)
    return widest is not None and widest.rank >= scope.rank


def _role_grants(db: Session, role: Role) -> dict[str, Scope]:
    if role.grants_all:
        return dict.fromkeys(catalogue.permissions(), Scope.ORGANIZATION)
    rows = db.execute(
        select(RoleGrant.permission_key, RoleGrant.scope).where(RoleGrant.role_id == role.id)
    )
    return {key: Scope(scope) for key, scope in sorted(rows)}


def _beyond_actor(db: Session, actor: Actor, grants: Mapping[str, Scope]) -> list[str]:
    held = _own_grants(db, actor)
    return [key for key, scope in grants.items() if not _can_grant(held, key, scope)]


def _check_not_self(actor: Actor, subject_user_id: uuid.UUID) -> None:
    broken = sod.violations(
        catalogue.sod_rules(),
        "authz.assignment.manage",
        actor.user_id,
        {"subject_user_id": subject_user_id},
        frozenset(),
    )
    if broken:
        raise PermissionDenied(broken[0].message)


def _user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound()
    return user


def _now_window(model: type[RoleAssignment], at: datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        model.ended_at.is_(None),
        model.valid_from <= at,
        or_(model.valid_to.is_(None), model.valid_to > at),
    )


# ------------------------------------------------------------------ catalogue sync


def catalogue_problems(db: Session) -> list[str]:
    """Differences between the code catalogue and the permissions table. Non-empty means
    a migration is missing; the app refuses to start."""
    code = {k: p.sensitive for k, p in catalogue.permissions().items()}
    stored = dict(db.execute(select(PermissionRow.key, PermissionRow.sensitive)).all())
    problems = [
        f"{k} is declared in code but has no migration" for k in sorted(code.keys() - stored)
    ]
    problems += [
        f"{k} is in the database but no policies.py declares it"
        for k in sorted(stored.keys() - code)
    ]
    problems += [
        f"{k} sensitivity differs between code and database"
        for k in sorted(code.keys() & stored.keys())
        if code[k] != stored[k]
    ]
    return problems


# ------------------------------------------------------------------ queries used by identity


def requires_mfa(db: Session, user_id: uuid.UUID) -> bool:
    return engine.load_grants(db, user_id).requires_mfa


def role_names(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """Current role names for each user, for lists."""
    if not user_ids:
        return {}
    result: dict[uuid.UUID, list[str]] = defaultdict(list)
    rows = db.execute(
        select(RoleAssignment.user_id, Role.name)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id.in_(user_ids), *_now_window(RoleAssignment, clock.now()))
        .order_by(Role.name)
    )
    for user_id, name in rows:
        if name not in result[user_id]:
            result[user_id].append(name)
    return dict(result)


def _super_admin_role(db: Session) -> Role:
    return db.scalars(select(Role).where(Role.template_key == SUPER_ADMIN)).one()


def is_super_admin(db: Session, user_id: uuid.UUID) -> bool:
    role = _super_admin_role(db)
    return bool(
        db.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.role_id == role.id,
                *_now_window(RoleAssignment, clock.now()),
            )
        )
    )


def _other_active_super_admins(db: Session, excluding: uuid.UUID) -> int:
    role = _super_admin_role(db)
    return (
        db.scalar(
            select(func.count(func.distinct(RoleAssignment.user_id)))
            .join(User, User.id == RoleAssignment.user_id)
            .where(
                RoleAssignment.role_id == role.id,
                RoleAssignment.user_id != excluding,
                User.status == "active",
                *_now_window(RoleAssignment, clock.now()),
            )
        )
        or 0
    )


def ensure_not_last_super_admin(db: Session, user_id: uuid.UUID) -> None:
    if is_super_admin(db, user_id) and _other_active_super_admins(db, user_id) == 0:
        raise Conflict(LAST_SUPER_ADMIN)


def grant_super_admin_at_setup(db: Session, user_id: uuid.UUID) -> None:
    """The first account gets the Super Administrator role. Only setup may do this
    without an actor."""
    db.add(
        RoleAssignment(
            user_id=user_id,
            role_id=_super_admin_role(db).id,
            valid_from=clock.now(),
            reason="Created during setup",
        )
    )
    _changed(db)
    _audit(
        db,
        None,
        "authz.assignment.create",
        entity_type="user",
        entity_id=str(user_id),
        reason="Super Administrator granted during setup",
    )


def revoke_delegations_for(db: Session, actor: Actor | None, user_id: uuid.UUID, why: str) -> int:
    """Revoke every live delegation to or from this person (on suspension, or by the
    system when they leave)."""
    now = clock.now()
    live = list(
        db.scalars(
            select(Delegation).where(
                or_(Delegation.from_user_id == user_id, Delegation.to_user_id == user_id),
                Delegation.revoked_at.is_(None),
                Delegation.ends_at > now,
            )
        )
    )
    for d in live:
        d.revoked_at, d.revoked_by = now, actor.user_id if actor else None
        _audit(
            db,
            actor,
            "authz.delegation.revoke",
            entity_type="delegation",
            entity_id=str(d.id),
            reason=why,
        )
    if live:
        _changed(db)
    return len(live)


# ------------------------------------------------------------------ roles


def _summaries(db: Session, roles: list[Role]) -> list[RoleSummary]:
    now = clock.now()
    members = dict(
        db.execute(
            select(RoleAssignment.role_id, func.count(func.distinct(RoleAssignment.user_id)))
            .where(*_now_window(RoleAssignment, now))
            .group_by(RoleAssignment.role_id)
        ).all()
    )
    counts = dict(
        db.execute(select(RoleGrant.role_id, func.count()).group_by(RoleGrant.role_id)).all()
    )
    total = len(catalogue.permissions())
    return [
        RoleSummary(
            id=r.id,
            name=r.name,
            description=r.description,
            template_key=r.template_key,
            protected=r.protected,
            grants_all=r.grants_all,
            requires_mfa=r.requires_mfa,
            member_count=members.get(r.id, 0),
            permission_count=total if r.grants_all else counts.get(r.id, 0),
        )
        for r in roles
    ]


def list_roles(db: Session) -> list[RoleSummary]:
    roles = list(db.scalars(select(Role).order_by(Role.protected.desc(), Role.name)))
    return _summaries(db, roles)


def get_role(db: Session, role_id: uuid.UUID) -> RoleDetail:
    role = db.get(Role, role_id)
    if role is None:
        raise NotFound()
    return RoleDetail(role=_summaries(db, [role])[0], grants=_role_grants(db, role))


def _validate_grants(grants: Mapping[str, Scope]) -> None:
    perms = catalogue.permissions()
    errors: dict[str, str] = {}
    for key, scope in grants.items():
        permission = perms.get(key)
        if permission is None:
            errors[key] = "This permission doesn't exist."
        elif scope not in permission.scopes:
            allowed = ", ".join(s.label for s in permission.scopes)
            errors[key] = f"{permission.label} can only be granted at: {allowed}."
    if errors:
        raise ValidationFailed("Some permissions can't be granted like that.", field_errors=errors)


def _validate_name(db: Session, name: str, role_id: uuid.UUID | None) -> str:
    name = " ".join(name.split())
    if not 2 <= len(name) <= 100:
        raise ValidationFailed(field_errors={"name": "Use between 2 and 100 characters."})
    clash = db.scalar(select(Role.id).where(func.lower(Role.name) == name.lower()))
    if clash is not None and clash != role_id:
        raise ValidationFailed(field_errors={"name": "Another role already has this name."})
    return name


def create_role(
    db: Session,
    actor: Actor,
    *,
    name: str,
    description: str,
    copy_from: uuid.UUID | None = None,
    requires_mfa: bool = False,
) -> uuid.UUID:
    require(db, actor, "authz.role.manage")
    name = _validate_name(db, name, None)
    grants: dict[str, Scope] = {}
    if copy_from is not None:
        source = db.get(Role, copy_from)
        if source is None:
            raise NotFound()
        grants = _role_grants(db, source)
        requires_mfa = requires_mfa or source.requires_mfa
    beyond = _beyond_actor(db, actor, grants)
    if beyond:
        raise PermissionDenied("You can't copy a role with permissions you don't hold yourself.")
    role = Role(name=name, description=description.strip(), requires_mfa=requires_mfa)
    db.add(role)
    db.flush()
    db.add_all(
        RoleGrant(role_id=role.id, permission_key=k, scope=s.value) for k, s in grants.items()
    )
    _changed(db)
    _audit(
        db,
        actor,
        "authz.role.create",
        entity_type="role",
        entity_id=str(role.id),
        changes={"name": {"from": None, "to": name}, "permissions": {"from": 0, "to": len(grants)}},
    )
    return role.id


def update_role(
    db: Session,
    actor: Actor,
    role_id: uuid.UUID,
    *,
    name: str,
    description: str,
    requires_mfa: bool,
    grants: Mapping[str, Scope],
) -> None:
    """Replace a role's details and grants. Only permissions the actor holds (at least as
    widely) may be added, changed or removed."""
    require(db, actor, "authz.role.manage")
    role = db.get(Role, role_id)
    if role is None:
        raise NotFound()
    if role.protected:
        raise Conflict(f"{role.name} is protected and can't be changed.")
    name = _validate_name(db, name, role.id)
    _validate_grants(grants)
    before = _role_grants(db, role)
    touched = {
        key: max(scope, before.get(key, scope), key=lambda s: s.rank)
        for key, scope in grants.items()
        if before.get(key) != scope
    }
    touched |= {key: scope for key, scope in before.items() if key not in grants}
    beyond = _beyond_actor(db, actor, touched)
    if beyond:
        labels = ", ".join(catalogue.get(k).label for k in beyond[:5])
        raise PermissionDenied(f"You can only change permissions you hold yourself: {labels}.")
    if requires_mfa != role.requires_mfa and not requires_mfa:
        # Turning off two-step verification weakens every member's sign-in.
        require(db, actor, "system.security.manage")
    changes: dict[str, object] = {}
    if name != role.name:
        changes["name"] = {"from": role.name, "to": name}
    if requires_mfa != role.requires_mfa:
        changes["requires_mfa"] = {"from": role.requires_mfa, "to": requires_mfa}
    for key in sorted(before.keys() | grants.keys()):
        old, new = before.get(key), grants.get(key)
        if old != new:
            changes[key] = {"from": old.value if old else None, "to": new.value if new else None}
    role.name, role.description, role.requires_mfa = name, description.strip(), requires_mfa
    existing = {
        g.permission_key: g
        for g in db.scalars(select(RoleGrant).where(RoleGrant.role_id == role.id))
    }
    for key, grant in existing.items():
        if key not in grants:
            db.delete(grant)
        elif grant.scope != grants[key].value:
            grant.scope = grants[key].value
    db.add_all(
        RoleGrant(role_id=role.id, permission_key=k, scope=s.value)
        for k, s in grants.items()
        if k not in existing
    )
    if changes:
        _changed(db)
        _audit(
            db,
            actor,
            "authz.role.update",
            entity_type="role",
            entity_id=str(role.id),
            changes=changes,
        )


def delete_role(db: Session, actor: Actor, role_id: uuid.UUID) -> None:
    require(db, actor, "authz.role.manage")
    role = db.get(Role, role_id)
    if role is None:
        raise NotFound()
    if role.protected or role.template_key is not None:
        raise Conflict("Built-in roles can't be deleted. Remove their permissions instead.")
    if db.scalar(
        select(func.count()).select_from(RoleAssignment).where(RoleAssignment.role_id == role.id)
    ):
        raise Conflict(
            "This role has been given to people, so it's kept for the record. "
            "Remove its permissions instead."
        )
    beyond = _beyond_actor(db, actor, _role_grants(db, role))
    if beyond:
        raise PermissionDenied("You can't delete a role with permissions you don't hold yourself.")
    _audit(
        db,
        actor,
        "authz.role.delete",
        entity_type="role",
        entity_id=str(role.id),
        changes={"name": {"from": role.name, "to": None}},
    )
    db.delete(role)
    _changed(db)


# ------------------------------------------------------------------ assignments


def _assignable(db: Session, actor: Actor, role: Role) -> bool:
    return not _beyond_actor(db, actor, _role_grants(db, role))


def assign_role(
    db: Session,
    actor: Actor,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    valid_from: datetime | None,
    valid_to: datetime | None,
    reason: str,
    department_id: uuid.UUID | None = None,
) -> uuid.UUID:
    require(db, actor, "authz.assignment.manage")
    _check_not_self(actor, user_id)
    user = _user(db, user_id)
    if user.status == "deactivated":
        raise Conflict("This account is deactivated.")
    role = db.get(Role, role_id)
    if role is None:
        raise NotFound()
    if not _assignable(db, actor, role):
        raise PermissionDenied(
            f"You can't give {role.name}: it includes permissions you don't hold."
        )
    reason = _reason(reason)
    now = clock.now()
    start = valid_from or now
    if valid_to is not None and valid_to <= max(start, now):
        raise ValidationFailed(
            field_errors={"valid_to": "The end must be after the start and in the future."}
        )
    overlap = db.scalar(
        select(func.count())
        .select_from(RoleAssignment)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.role_id == role_id,
            RoleAssignment.ended_at.is_(None),
            or_(RoleAssignment.valid_to.is_(None), RoleAssignment.valid_to > start),
            *([RoleAssignment.valid_from < valid_to] if valid_to else []),
        )
    )
    if overlap:
        raise Conflict(f"{user.full_name} already has {role.name} for some of that period.")
    assignment = RoleAssignment(
        user_id=user_id,
        role_id=role_id,
        valid_from=start,
        valid_to=valid_to,
        reason=reason,
        assigned_by=actor.user_id,
        department_id=department_id,
    )
    db.add(assignment)
    db.flush()
    _changed(db)
    _audit(
        db,
        actor,
        "authz.assignment.create",
        entity_type="user",
        entity_id=str(user_id),
        reason=reason,
        changes={
            "role": {"from": None, "to": role.name},
            "valid_from": {"from": None, "to": start.isoformat()},
            "valid_to": {"from": None, "to": valid_to.isoformat() if valid_to else None},
        },
    )
    return assignment.id


def end_assignment(db: Session, actor: Actor, assignment_id: uuid.UUID, reason: str) -> None:
    require(db, actor, "authz.assignment.manage")
    assignment = db.get(RoleAssignment, assignment_id)
    if assignment is None or assignment.ended_at is not None:
        raise NotFound()
    _check_not_self(actor, assignment.user_id)
    role = db.get(Role, assignment.role_id)
    if role is None:
        raise NotFound()
    if not _assignable(db, actor, role):
        raise PermissionDenied(
            f"You can't remove {role.name}: it includes permissions you don't hold."
        )
    reason = _reason(reason)
    now = clock.now()
    current = assignment.valid_from <= now and (
        assignment.valid_to is None or assignment.valid_to > now
    )
    if role.template_key == SUPER_ADMIN and current:
        user = _user(db, assignment.user_id)
        if user.status == "active" and _other_active_super_admins(db, user.id) == 0:
            raise Conflict(LAST_SUPER_ADMIN)
    assignment.ended_at, assignment.ended_by = now, actor.user_id
    _changed(db)
    _audit(
        db,
        actor,
        "authz.assignment.end",
        entity_type="user",
        entity_id=str(assignment.user_id),
        reason=reason,
        changes={"role": {"from": role.name, "to": None}},
    )


EMPLOYEE_TEMPLATE = "employee"
# The only permission the Employee role may grant beyond `self` (the colleague directory).
EMPLOYEE_ROLE_WIDER = frozenset({"hr.employee.read"})
EMPLOYEE_ROLE_CHANGED = (
    "The Employee role now includes more than self-service access, so it can't be given "
    "automatically. Ask an administrator to assign their roles."
)


def give_employee_role(db: Session, actor: Actor, user_id: uuid.UUID) -> bool:
    """Give a newly invited employee the role copied from the Employee template (ADR 0017).
    HR can't hold every self-scoped permission, so this skips "can't grant what you don't
    hold", but only while that role grants nothing wider than self apart from the
    directory. Returns False when the role no longer exists or is already held."""
    role = db.scalars(select(Role).where(Role.template_key == EMPLOYEE_TEMPLATE)).one_or_none()
    if role is None:
        return False
    grants = _role_grants(db, role)
    wider = [k for k, s in grants.items() if s is not Scope.SELF and k not in EMPLOYEE_ROLE_WIDER]
    if role.grants_all or wider:
        raise Conflict(EMPLOYEE_ROLE_CHANGED)
    now = clock.now()
    held = db.scalar(
        select(func.count())
        .select_from(RoleAssignment)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.role_id == role.id,
            *_now_window(RoleAssignment, now),
        )
    )
    if held:
        return False
    reason = "Invited from their HR record"
    db.add(
        RoleAssignment(
            user_id=user_id,
            role_id=role.id,
            valid_from=now,
            reason=reason,
            assigned_by=actor.user_id,
        )
    )
    db.flush()
    _changed(db)
    _audit(
        db,
        actor,
        "authz.assignment.create",
        entity_type="user",
        entity_id=str(user_id),
        reason=reason,
        changes={"role": {"from": None, "to": role.name}},
    )
    return True


def end_all_access(db: Session, user_id: uuid.UUID, why: str) -> None:
    """The system ends every role assignment and delegation of someone who has left."""
    now = clock.now()
    live = list(
        db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.ended_at.is_(None),
                or_(RoleAssignment.valid_to.is_(None), RoleAssignment.valid_to > now),
            )
        )
    )
    for assignment in live:
        assignment.ended_at = now
        role = db.get(Role, assignment.role_id)
        _audit(
            db,
            None,
            "authz.assignment.end",
            entity_type="user",
            entity_id=str(user_id),
            reason=why,
            changes={"role": {"from": role.name if role else None, "to": None}},
        )
    if live:
        _changed(db)
    revoke_delegations_for(db, None, user_id, why)


def assignment_owner(db: Session, assignment_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(RoleAssignment.user_id).where(RoleAssignment.id == assignment_id))


def _assignment_status(a: RoleAssignment, now: datetime) -> str:
    if a.ended_at is not None or (a.valid_to is not None and a.valid_to <= now):
        return "ended"
    return "upcoming" if a.valid_from > now else "current"


# ------------------------------------------------------------------ delegations


def delegation_max_days(db: Session) -> int:
    return db.scalars(select(SecuritySettings.delegation_max_days)).one()


def _delegation_period(db: Session, starts_at: datetime | None, ends_at: datetime) -> datetime:
    now = clock.now()
    start = starts_at or now
    if ends_at <= max(start, now):
        raise ValidationFailed(
            field_errors={"ends_at": "The end must be after the start and in the future."}
        )
    limit = delegation_max_days(db)
    if ends_at - start > timedelta(days=limit):
        raise ValidationFailed(
            field_errors={"ends_at": f"A delegation can last at most {limit} days."}
        )
    return start


def create_delegation(
    db: Session,
    actor: Actor,
    *,
    to_user_id: uuid.UUID,
    permission_keys: list[str],
    scope: Scope,
    starts_at: datetime | None,
    ends_at: datetime,
    reason: str,
    from_user_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Lend some of your permissions to a colleague for a fixed period (for example while
    you're on leave). With authz.delegation.manage you may set one up for someone else."""
    delegator = from_user_id or actor.user_id
    if delegator != actor.user_id:
        require(db, actor, "authz.delegation.manage")
        if to_user_id == actor.user_id:
            raise PermissionDenied("You can't set up a delegation to yourself on someone's behalf.")
    if to_user_id == delegator:
        raise ValidationFailed(field_errors={"to_user_id": "Choose someone else."})
    target = _user(db, to_user_id)
    if target.status != "active":
        raise ValidationFailed(
            field_errors={"to_user_id": "Only active colleagues can receive a delegation."}
        )
    source = _user(db, delegator)
    if source.status != "active":
        raise Conflict(f"{source.full_name}'s account isn't active.")
    if not permission_keys:
        raise ValidationFailed(field_errors={"permissions": "Choose at least one permission."})
    reason = _reason(reason)
    start = _delegation_period(db, starts_at, ends_at)
    held = engine.load_grants(db, delegator, include_delegations=False)
    ids: list[uuid.UUID] = []
    for key in sorted(set(permission_keys)):
        permission = catalogue.get(key)
        if permission.module in NOT_DELEGABLE_MODULES:
            raise ValidationFailed(f"{permission.label} can't be delegated.")
        if scope not in permission.scopes:
            raise ValidationFailed(f"{permission.label} can't be delegated at {scope.label} scope.")
        if not _can_grant(held, key, scope):
            raise PermissionDenied(
                f"{source.full_name} doesn't hold {permission.label} at {scope.label} scope."
            )
        d = Delegation(
            from_user_id=delegator,
            to_user_id=to_user_id,
            permission_key=key,
            scope=scope.value,
            starts_at=start,
            ends_at=ends_at,
            reason=reason,
            created_by=actor.user_id,
        )
        db.add(d)
        db.flush()
        ids.append(d.id)
        _audit(
            db,
            actor,
            "authz.delegation.create",
            entity_type="delegation",
            entity_id=str(d.id),
            reason=reason,
            changes={
                "permission": {"from": None, "to": key},
                "scope": {"from": None, "to": scope.value},
                "from_user": {"from": None, "to": str(delegator)},
                "to_user": {"from": None, "to": str(to_user_id)},
                "ends_at": {"from": None, "to": ends_at.isoformat()},
            },
        )
    _changed(db)
    return ids


def revoke_delegation(db: Session, actor: Actor, delegation_id: uuid.UUID) -> None:
    d = db.get(Delegation, delegation_id)
    if d is None:
        raise NotFound()
    involved = actor.user_id in {d.from_user_id, d.to_user_id}
    if not involved and not engine.check(db, actor.user_id, "authz.delegation.manage"):
        raise NotFound()  # out of scope: don't reveal it exists
    if d.revoked_at is not None or d.ends_at <= clock.now():
        raise Conflict("This delegation has already ended.")
    d.revoked_at, d.revoked_by = clock.now(), actor.user_id
    _changed(db)
    _audit(db, actor, "authz.delegation.revoke", entity_type="delegation", entity_id=str(d.id))


def _delegation_status(d: Delegation, now: datetime) -> str:
    if d.revoked_at is not None:
        return "revoked"
    if d.ends_at <= now:
        return "ended"
    return "upcoming" if d.starts_at > now else "active"


def _delegation_views(db: Session, rows: list[Delegation]) -> list[DelegationView]:
    ids = {d.from_user_id for d in rows} | {d.to_user_id for d in rows}
    names = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(ids))).all())
        if ids
        else {}
    )
    now = clock.now()
    perms = catalogue.permissions()
    return [
        DelegationView(
            id=d.id,
            from_user_id=d.from_user_id,
            from_name=names.get(d.from_user_id, "—"),
            to_user_id=d.to_user_id,
            to_name=names.get(d.to_user_id, "—"),
            permission_key=d.permission_key,
            permission_label=perms[d.permission_key].label
            if d.permission_key in perms
            else d.permission_key,
            scope=Scope(d.scope),
            starts_at=d.starts_at,
            ends_at=d.ends_at,
            reason=d.reason,
            revoked_at=d.revoked_at,
            status=_delegation_status(d, now),
        )
        for d in rows
    ]


def list_delegations(
    db: Session,
    actor: Actor,
    *,
    everyone: bool = False,
    include_past: bool = False,
) -> list[DelegationView]:
    stmt = select(Delegation).order_by(Delegation.starts_at.desc())
    if everyone:
        require(db, actor, "authz.delegation.manage")
    else:
        stmt = stmt.where(
            or_(Delegation.from_user_id == actor.user_id, Delegation.to_user_id == actor.user_id)
        )
    if not include_past:
        stmt = stmt.where(Delegation.revoked_at.is_(None), Delegation.ends_at > clock.now())
    return _delegation_views(db, list(db.scalars(stmt.limit(500))))


def delegable_permissions(db: Session, user_id: uuid.UUID) -> list[EffectivePermission]:
    """What this person could lend: their own role grants, minus non-delegable ones."""
    return [
        p
        for p in _effective(db, engine.load_grants(db, user_id, include_delegations=False))
        if p.module not in NOT_DELEGABLE_MODULES
    ]


# ------------------------------------------------------------------ access overview


def _effective(db: Session, grants: engine.Grants) -> list[EffectivePermission]:
    perms = catalogue.permissions()
    names: dict[str, str] = {}
    obo = {
        g.on_behalf_of for items in grants.by_permission.values() for g in items if g.on_behalf_of
    }
    if obo:
        names = {
            str(i): n
            for i, n in db.execute(select(User.id, User.full_name).where(User.id.in_(obo)))
        }
    result = []
    for key in sorted(grants.permissions):
        permission = perms[key]
        sources = []
        for g in grants.by_permission[key]:
            label = (
                g.source.removeprefix("role:")
                if g.source.startswith("role:")
                else f"Delegated by {names.get(str(g.on_behalf_of), 'a colleague')}"
            )
            if label not in sources:
                sources.append(label)
        widest = grants.widest(key) or Scope.SELF
        result.append(
            EffectivePermission(
                key, permission.label, permission.module, permission.sensitive, widest, sources
            )
        )
    return result


def user_access(db: Session, actor: Actor, user_id: uuid.UUID) -> UserAccess:
    user = _user(db, user_id)
    now = clock.now()
    rows = db.execute(
        select(RoleAssignment, Role)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == user_id)
        .order_by(RoleAssignment.valid_from.desc())
    ).all()
    assigners = {a.assigned_by for a, _ in rows if a.assigned_by}
    names = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(assigners))).all())
        if assigners
        else {}
    )
    assignments = [
        AssignmentView(
            id=a.id,
            role_id=r.id,
            role_name=r.name,
            protected=r.protected,
            valid_from=a.valid_from,
            valid_to=a.valid_to,
            reason=a.reason,
            assigned_by=names.get(a.assigned_by) if a.assigned_by else "Setup",
            ended_at=a.ended_at,
            status=_assignment_status(a, now),
        )
        for a, r in rows
    ]
    order = {"current": 0, "upcoming": 1, "ended": 2}
    assignments.sort(key=lambda a: (order[a.status], a.role_name))
    delegations = _delegation_views(
        db,
        list(
            db.scalars(
                select(Delegation)
                .where(
                    or_(Delegation.from_user_id == user_id, Delegation.to_user_id == user_id),
                    Delegation.revoked_at.is_(None),
                    Delegation.ends_at > now,
                )
                .order_by(Delegation.starts_at)
            )
        ),
    )
    grants = engine.load_grants(db, user_id)
    held_roles = {a.role_id for a in assignments if a.status != "ended"}
    assignable = (
        [
            r
            for r in list_roles(db)
            if r.id not in held_roles and _assignable(db, actor, db.get(Role, r.id))  # type: ignore[arg-type]
        ]
        if user_id != actor.user_id
        else []
    )
    return UserAccess(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        status=user.status,
        assignments=assignments,
        delegations_in=[d for d in delegations if d.to_user_id == user_id],
        delegations_out=[d for d in delegations if d.from_user_id == user_id],
        effective=_effective(db, grants),
        requires_mfa=grants.requires_mfa,
        roles_assignable=assignable,
    )


def delegators(db: Session, to_user_id: uuid.UUID, permission: str) -> set[uuid.UUID]:
    """Users who have delegated `permission` to this user, right now (e.g. to decide
    approval steps named on them)."""
    now = clock.now()
    return set(
        db.scalars(
            select(Delegation.from_user_id).where(
                Delegation.to_user_id == to_user_id,
                Delegation.permission_key == permission,
                Delegation.starts_at <= now,
                Delegation.ends_at > now,
                Delegation.revoked_at.is_(None),
            )
        )
    )
