import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.authz.permissions import Scope


@dataclass(frozen=True, slots=True)
class RoleSummary:
    id: uuid.UUID
    name: str
    description: str
    template_key: str | None
    protected: bool
    grants_all: bool
    requires_mfa: bool
    member_count: int
    permission_count: int


@dataclass(frozen=True, slots=True)
class RoleDetail:
    role: RoleSummary
    grants: dict[str, Scope]  # every permission for grants_all roles


@dataclass(frozen=True, slots=True)
class AssignmentView:
    id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    protected: bool
    valid_from: datetime
    valid_to: datetime | None
    reason: str
    assigned_by: str | None
    ended_at: datetime | None
    status: str  # current | upcoming | ended


@dataclass(frozen=True, slots=True)
class DelegationView:
    id: uuid.UUID
    from_user_id: uuid.UUID
    from_name: str
    to_user_id: uuid.UUID
    to_name: str
    permission_key: str
    permission_label: str
    scope: Scope
    starts_at: datetime
    ends_at: datetime
    reason: str
    revoked_at: datetime | None
    status: str  # active | upcoming | ended | revoked


@dataclass(frozen=True, slots=True)
class EffectivePermission:
    key: str
    label: str
    module: str
    sensitive: bool
    scope: Scope
    sources: list[str]


@dataclass(frozen=True, slots=True)
class UserAccess:
    user_id: uuid.UUID
    full_name: str
    email: str
    status: str
    assignments: list[AssignmentView]
    delegations_in: list[DelegationView]
    delegations_out: list[DelegationView]
    effective: list[EffectivePermission]
    requires_mfa: bool
    roles_assignable: list[RoleSummary] = field(default_factory=list)
