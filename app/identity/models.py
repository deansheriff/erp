"""Identity tables (docs/IDENTITY.md section 2)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class Installation(IdMixin, Base):
    __tablename__ = "installation"
    __table_args__ = (CheckConstraint("is_singleton", name="one_row"),)

    is_singleton: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), unique=True
    )
    installed_at: Mapped[datetime | None]
    setup_token_hash: Mapped[str | None] = mapped_column(String(64))
    setup_token_expires_at: Mapped[datetime | None]


class SecuritySettings(IdMixin, Base):
    """Values confirmed by the product owner on 2026-11-17 (docs/IDENTITY.md section 1)."""

    __tablename__ = "security_settings"
    __table_args__ = (CheckConstraint("is_singleton", name="one_row"),)

    is_singleton: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), unique=True
    )
    session_idle_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    session_absolute_hours: Mapped[int] = mapped_column(SmallInteger, server_default="12")
    lockout_threshold: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    lockout_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    password_min_length: Mapped[int] = mapped_column(SmallInteger, server_default="12")
    invite_ttl_hours: Mapped[int] = mapped_column(SmallInteger, server_default="72")
    reset_ttl_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    ip_attempt_limit: Mapped[int] = mapped_column(SmallInteger, server_default="20")
    ip_attempt_window_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    # Longest a delegation may last (confirmed 2026-11-18).
    delegation_max_days: Mapped[int] = mapped_column(SmallInteger, server_default="90")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class User(IdMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'deactivated')", name="status"
        ),
    )

    email: Mapped[str] = mapped_column(String(254), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[datetime | None]
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(String(255))
    mfa_enabled_at: Mapped[datetime | None]
    # Last accepted TOTP time step, so a code can't be replayed within its window.
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger)
    failed_login_count: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def mfa_enabled(self) -> bool:
        return self.mfa_enabled_at is not None


class UserSession(IdMixin, Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    mfa_satisfied: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str | None] = mapped_column(String(40))


class UserToken(IdMixin, Base):
    """Single-use emailed tokens: invitations and password resets."""

    __tablename__ = "user_tokens"
    __table_args__ = (
        CheckConstraint("purpose IN ('invite', 'password_reset')", name="purpose"),
        Index("ix_user_tokens_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MfaRecoveryCode(IdMixin, Base):
    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (Index("ix_mfa_recovery_codes_user_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(255))
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LoginAttempt(IdMixin, Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_ip_at", "ip", "attempted_at"),)

    ip: Mapped[str] = mapped_column(INET)
    attempted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    succeeded: Mapped[bool] = mapped_column(Boolean)


class ApiKey(IdMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_user_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Enforced as a subset of the owner's permissions from Phase 3.
    permissions: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
