"""The policy engine (ADR 0013): one way to decide who may do what to which record.

    decision = check(db, actor, "expense.claim.approve", resource)
    query = select(Claim).where(scope_filter(db, actor, "expense.claim.read", Claim))

Grants are read live from the database (never from the session or a token), cached for
the request and, when Redis is reachable, across requests under the authz version.
"""

import json
import logging
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol

from sqlalchemy import ColumnElement, false, or_, select, true
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.authz import catalogue
from app.authz.models import AuthzState, Delegation, Role, RoleAssignment, RoleGrant
from app.authz.permissions import Scope
from app.core import clock

log = logging.getLogger("app.authz")


# ------------------------------------------------------------------ organisation structure


class OrgResolver(Protocol):
    """Answers questions about the organisation chart. Phase 4 supplies the real one."""

    def employee_of(self, db: Session, user_id: uuid.UUID) -> uuid.UUID | None: ...
    def team_employees(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]: ...
    def departments_of(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]: ...
    def departments_under(self, db: Session, department_id: uuid.UUID) -> set[uuid.UUID]: ...
    def department_employees(
        self, db: Session, department_ids: set[uuid.UUID], at: datetime
    ) -> set[uuid.UUID]: ...


class NoOrgStructure:
    """Until departments and positions exist, team and department scopes reach nobody
    but the person themselves."""

    def employee_of(self, db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
        return None

    def team_employees(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        return set()

    def departments_of(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        return set()

    def departments_under(self, db: Session, department_id: uuid.UUID) -> set[uuid.UUID]:
        return {department_id}

    def department_employees(
        self, db: Session, department_ids: set[uuid.UUID], at: datetime
    ) -> set[uuid.UUID]:
        return set()


_resolver: OrgResolver = NoOrgStructure()


def set_org_resolver(resolver: OrgResolver) -> None:
    global _resolver  # noqa: PLW0603  (configured once at start-up, or by tests)
    _resolver = resolver


def org_resolver() -> OrgResolver:
    return _resolver


# ------------------------------------------------------------------ resources


@dataclass(frozen=True, slots=True)
class Resource:
    """The facts about a record that authorisation needs."""

    owner_user_id: uuid.UUID | None = None
    owner_employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthzColumns:
    """How a model exposes ownership to `scope_filter`. Set as `__authz__` on the model."""

    owner_user: InstrumentedAttribute[Any] | None = None
    owner_employee: InstrumentedAttribute[Any] | None = None
    department: InstrumentedAttribute[Any] | None = None


# ------------------------------------------------------------------ grants


@dataclass(frozen=True, slots=True)
class Grant:
    permission: str
    scope: Scope
    source: str  # "role:<name>" or "delegation:<from user id>"
    department_restriction: uuid.UUID | None = None
    on_behalf_of: uuid.UUID | None = None  # delegations: scope seen from the delegator


@dataclass(slots=True)
class Grants:
    user_id: uuid.UUID
    by_permission: dict[str, list[Grant]] = field(default_factory=lambda: defaultdict(list))
    requires_mfa: bool = False
    role_names: list[str] = field(default_factory=list)
    # When these grants next change by the clock alone (an acting role ends, a delegation
    # starts). Caches must not outlive it.
    next_change: datetime | None = None

    def note_change(self, when: datetime | None) -> None:
        if when is not None and (self.next_change is None or when < self.next_change):
            self.next_change = when

    def holds(self, permission: str) -> bool:
        return bool(self.by_permission.get(permission))

    def widest(self, permission: str) -> Scope | None:
        grants = self.by_permission.get(permission) or []
        return max((g.scope for g in grants), key=lambda s: s.rank, default=None)

    @property
    def permissions(self) -> frozenset[str]:
        return frozenset(k for k, v in self.by_permission.items() if v)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


_request_cache: ContextVar[dict[tuple[uuid.UUID, int], Grants] | None] = ContextVar(
    "kx_authz_cache", default=None
)


def begin_request_cache() -> None:
    """Called by the access guard at the start of each request."""
    _request_cache.set({})


def authz_version(db: Session) -> int:
    return db.scalar(select(AuthzState.version)) or 0


def bump_version(db: Session) -> None:
    state = db.scalars(select(AuthzState).with_for_update()).one()
    state.version += 1


def _active_assignments(
    db: Session, user_id: uuid.UUID, at: datetime
) -> list[tuple[RoleAssignment, Role]]:
    return list(
        db.execute(
            select(RoleAssignment, Role)
            .join(Role, Role.id == RoleAssignment.role_id)
            .where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.ended_at.is_(None),
                RoleAssignment.valid_from <= at,
                (RoleAssignment.valid_to.is_(None)) | (RoleAssignment.valid_to > at),
            )
        )
    )


def _load(
    db: Session, user_id: uuid.UUID, at: datetime, *, with_delegations: bool = True
) -> Grants:
    grants = Grants(user_id)
    for upcoming in db.scalars(
        select(RoleAssignment.valid_from).where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.ended_at.is_(None),
            RoleAssignment.valid_from > at,
        )
    ):
        grants.note_change(upcoming)
    for assignment, role in _active_assignments(db, user_id, at):
        grants.note_change(assignment.valid_to)
        grants.role_names.append(role.name)
        grants.requires_mfa = grants.requires_mfa or role.requires_mfa
        source = f"role:{role.name}"
        if role.grants_all:
            for key in catalogue.permissions():
                grants.by_permission[key].append(Grant(key, Scope.ORGANIZATION, source))
            continue
        for key, scope in db.execute(
            select(RoleGrant.permission_key, RoleGrant.scope).where(RoleGrant.role_id == role.id)
        ):
            grants.by_permission[key].append(
                Grant(key, Scope(scope), source, department_restriction=assignment.department_id)
            )
    if with_delegations:
        for starts in db.scalars(
            select(Delegation.starts_at).where(
                Delegation.to_user_id == user_id,
                Delegation.revoked_at.is_(None),
                Delegation.starts_at > at,
            )
        ):
            grants.note_change(starts)
        for d in db.scalars(
            select(Delegation).where(
                Delegation.to_user_id == user_id,
                Delegation.revoked_at.is_(None),
                Delegation.starts_at <= at,
                Delegation.ends_at > at,
            )
        ):
            grants.note_change(d.ends_at)
            # A delegation only carries what the delegator still holds right now, and
            # never more widely than they hold it.
            widest = _load(db, d.from_user_id, at, with_delegations=False).widest(d.permission_key)
            if widest is None:
                continue
            scope = Scope(d.scope) if Scope(d.scope).rank <= widest.rank else widest
            grants.by_permission[d.permission_key].append(
                Grant(
                    d.permission_key,
                    scope,
                    f"delegation:{d.from_user_id}",
                    on_behalf_of=d.from_user_id,
                )
            )
    grants.role_names.sort()
    return grants


def load_grants(db: Session, user_id: uuid.UUID, *, include_delegations: bool = True) -> Grants:
    at = clock.now()
    if not include_delegations:
        return _load(db, user_id, at, with_delegations=False)
    version = authz_version(db)
    cache = _request_cache.get()
    cache_key = (user_id, version)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    grants = _from_redis(user_id, version) or _load(db, user_id, at)
    _to_redis(grants, version)
    if cache is not None:
        cache[cache_key] = grants
    return grants


# ------------------------------------------------------------------ Redis cache (optional)

_redis_client: Any = None
_redis_disabled = False
_TTL_SECONDS = 300


def _redis() -> Any:
    global _redis_client, _redis_disabled  # noqa: PLW0603
    if _redis_disabled:
        return None
    if _redis_client is None:
        try:
            import redis  # noqa: PLC0415

            from app.core.config import get_settings  # noqa: PLC0415

            _redis_client = redis.Redis.from_url(
                get_settings().redis_url, socket_timeout=0.2, socket_connect_timeout=0.2
            )
            _redis_client.ping()
        except Exception:  # noqa: BLE001  (cache is optional; fall back to the database)
            log.warning("authz_cache_unavailable")
            _redis_disabled = True
            return None
    return _redis_client


def _from_redis(user_id: uuid.UUID, version: int) -> Grants | None:
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(f"authz:{user_id}:{version}")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    data = json.loads(raw)
    grants = Grants(user_id, requires_mfa=data["mfa"], role_names=data["roles"])
    if data.get("next"):
        grants.next_change = datetime.fromisoformat(data["next"])
    for g in data["grants"]:
        grants.by_permission[g["p"]].append(
            Grant(
                g["p"],
                Scope(g["s"]),
                g["src"],
                uuid.UUID(g["dep"]) if g["dep"] else None,
                uuid.UUID(g["obo"]) if g["obo"] else None,
            )
        )
    return grants


def _to_redis(grants: Grants, version: int) -> None:
    client = _redis()
    if client is None:
        return
    payload = {
        "mfa": grants.requires_mfa,
        "roles": grants.role_names,
        "next": grants.next_change.isoformat() if grants.next_change else None,
        "grants": [
            {
                "p": g.permission,
                "s": g.scope.value,
                "src": g.source,
                "dep": str(g.department_restriction) if g.department_restriction else None,
                "obo": str(g.on_behalf_of) if g.on_behalf_of else None,
            }
            for items in grants.by_permission.values()
            for g in items
        ],
    }
    ttl = _TTL_SECONDS
    if grants.next_change is not None:
        ttl = min(ttl, int((grants.next_change - clock.now()).total_seconds()))
    if ttl <= 0:
        return
    try:
        client.setex(f"authz:{grants.user_id}:{version}", ttl, json.dumps(payload))
    except Exception:  # noqa: BLE001
        return


# ------------------------------------------------------------------ decisions


def _departments_reached(
    db: Session, resolver: OrgResolver, subject: uuid.UUID, at: datetime
) -> set[uuid.UUID]:
    """The subject's departments and everything under them."""
    departments: set[uuid.UUID] = set()
    for dep in resolver.departments_of(db, subject, at):
        departments |= resolver.departments_under(db, dep)
    return departments


def _in_departments(
    db: Session, resolver: OrgResolver, subject: uuid.UUID, resource: Resource, at: datetime
) -> bool:
    departments = _departments_reached(db, resolver, subject, at)
    if resource.department_id is not None:
        return resource.department_id in departments
    # Records without a department of their own (an employee) belong to the departments
    # of their owner's positions.
    return bool(departments) and resource.owner_employee_id in resolver.department_employees(
        db, departments, at
    )


def _covers(db: Session, grant: Grant, actor_id: uuid.UUID, resource: Resource) -> bool:
    resolver = org_resolver()
    at = clock.now()
    subject = grant.on_behalf_of or actor_id
    # A grant restricted to a department never reaches records outside it, including
    # records with no department at all.
    if grant.department_restriction is not None and (
        resource.department_id is None
        or resource.department_id
        not in resolver.departments_under(db, grant.department_restriction)
    ):
        return False
    if grant.scope is Scope.ORGANIZATION:
        return True
    subject_employee = resolver.employee_of(db, subject)
    owns = resource.owner_user_id == subject or (
        subject_employee is not None and resource.owner_employee_id == subject_employee
    )
    if owns:
        return True
    if grant.scope is Scope.TEAM:
        return resource.owner_employee_id in resolver.team_employees(db, subject, at)
    if grant.scope is Scope.DEPARTMENT:
        return _in_departments(db, resolver, subject, resource, at)
    return False


def check(
    db: Session, user_id: uuid.UUID, permission: str, resource: Resource | None = None
) -> Decision:
    """Allow or deny, with a reason. Unknown permissions raise (a programming error)."""
    catalogue.get(permission)
    grants = load_grants(db, user_id).by_permission.get(permission) or []
    if not grants:
        return Decision(False, f"no grant of {permission}")
    if resource is None:
        return Decision(True, f"holds {permission} via {grants[0].source}")
    for grant in grants:
        if _covers(db, grant, user_id, resource):
            return Decision(True, f"{grant.scope.value} scope via {grant.source}")
    return Decision(False, f"{permission} held, but the record is outside its scope")


class HasAuthzColumns(Protocol):
    __authz__: ClassVar[AuthzColumns]


def _grant_clause(
    db: Session,
    grant: Grant,
    *,
    subject: uuid.UUID,
    cols: AuthzColumns,
    resolver: OrgResolver,
    at: datetime,
) -> ColumnElement[bool]:
    if grant.scope is Scope.ORGANIZATION:
        return true()
    parts: list[ColumnElement[bool]] = []
    if cols.owner_user is not None:
        parts.append(cols.owner_user == subject)
    employee = resolver.employee_of(db, subject)
    if cols.owner_employee is not None and employee is not None:
        parts.append(cols.owner_employee == employee)
    if grant.scope is Scope.TEAM and cols.owner_employee is not None:
        team = resolver.team_employees(db, subject, at)
        if team:
            parts.append(cols.owner_employee.in_(team))
    if grant.scope is Scope.DEPARTMENT:
        departments = _departments_reached(db, resolver, subject, at)
        if departments and cols.department is not None:
            parts.append(cols.department.in_(departments))
        elif departments and cols.owner_employee is not None:
            members = resolver.department_employees(db, departments, at)
            if members:
                parts.append(cols.owner_employee.in_(members))
    return or_(*parts) if parts else false()


def scope_filter(
    db: Session, user_id: uuid.UUID, permission: str, model: type[HasAuthzColumns]
) -> ColumnElement[bool]:
    """A WHERE clause limiting `model` rows to those `permission` covers for this person.
    Lists and exports must use this; never filter in Python after loading."""
    catalogue.get(permission)
    cols = model.__authz__
    resolver = org_resolver()
    at = clock.now()
    clauses: list[ColumnElement[bool]] = []
    for grant in load_grants(db, user_id).by_permission.get(permission) or []:
        clause = _grant_clause(
            db, grant, subject=grant.on_behalf_of or user_id, cols=cols, resolver=resolver, at=at
        )
        if grant.department_restriction is not None:
            if cols.department is None:
                clause = false()  # can't prove the row is inside the department
            else:
                allowed = resolver.departments_under(db, grant.department_restriction)
                clause = clause & cols.department.in_(allowed)
        clauses.append(clause)
    return or_(*clauses) if clauses else false()
