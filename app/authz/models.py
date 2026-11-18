"""Authorisation tables (ADR 0013)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

SCOPE_CHECK = "scope IN ('self', 'team', 'department', 'organization')"


class PermissionRow(Base):
    """Mirror of the code catalogue; migrations keep it in sync."""

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    module: Mapped[str] = mapped_column(String(40))
    sensitive: Mapped[bool] = mapped_column(Boolean, server_default="false")


class Role(IdMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, server_default="")
    template_key: Mapped[str | None] = mapped_column(String(60), unique=True)
    requires_mfa: Mapped[bool] = mapped_column(Boolean, server_default="false")
    protected: Mapped[bool] = mapped_column(Boolean, server_default="false")
    grants_all: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RoleGrant(Base):
    __tablename__ = "role_grants"
    __table_args__ = (CheckConstraint(SCOPE_CHECK, name="scope"),)

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(String(20))


class RoleAssignment(IdMixin, Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        Index("ix_role_assignments_user_id", "user_id"),
        Index("ix_role_assignments_role_id", "role_id"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="period"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    # Optional restriction to one department (and its sub-departments). The departments
    # table arrives in Phase 4, which adds the foreign key.
    department_id: Mapped[uuid.UUID | None]
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime | None]
    reason: Mapped[str] = mapped_column(Text)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    ended_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class Delegation(IdMixin, Base):
    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint(SCOPE_CHECK, name="scope"),
        CheckConstraint("ends_at > starts_at", name="period"),
        CheckConstraint("from_user_id <> to_user_id", name="not_self"),
        Index("ix_delegations_to_user_id", "to_user_id"),
        Index("ix_delegations_from_user_id", "from_user_id"),
    )

    from_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    to_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    permission_key: Mapped[str] = mapped_column(ForeignKey("permissions.key", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(20))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class AuthzState(IdMixin, Base):
    """A single counter bumped by every change to roles, grants, assignments or
    delegations. Cached grants are keyed on it, so they're never stale."""

    __tablename__ = "authz_state"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        UniqueConstraint("is_singleton", name="uq_authz_state_is_singleton"),
    )

    is_singleton: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    version: Mapped[int] = mapped_column(BigInteger, server_default="1")
