import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making the request. Carries identity only, never permissions (ADR 0009)."""

    user_id: uuid.UUID
    email: str
    full_name: str
    is_super_admin: bool
    mfa_enabled: bool
    mfa_required: bool
    mfa_satisfied: bool
    via: Literal["session", "api_key"]
    session_id: uuid.UUID | None = None
    csrf_token: str | None = None
    api_key_id: uuid.UUID | None = None

    @property
    def label(self) -> str:
        return f"{self.full_name} <{self.email}>"


LoginStatus = Literal["ok", "invalid", "locked", "rate_limited"]


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: LoginStatus
    user_id: uuid.UUID | None = None
    attempts_left: int | None = None
    locked_minutes: int | None = None


class InviteInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr


class AdminAccountInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(max_length=200)
    password_confirm: str = Field(max_length=200)
    mfa_code: str = Field(max_length=12)


@dataclass(frozen=True, slots=True)
class MfaEnrolment:
    secret_grouped: str
    qr_svg: str


@dataclass(frozen=True, slots=True)
class SessionView:
    id: uuid.UUID
    browser: str
    device: Literal["desktop", "mobile"]
    ip: str | None
    last_seen_at: datetime
    is_current: bool


@dataclass(frozen=True, slots=True)
class ApiKeyView:
    id: uuid.UUID
    name: str
    display: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class SecurityOverview:
    password_changed_at: datetime | None
    mfa_enabled_at: datetime | None
    recovery_codes_left: int
    sessions: list[SessionView]
    api_keys: list[ApiKeyView]


@dataclass(frozen=True, slots=True)
class UserRow:
    id: uuid.UUID
    full_name: str
    email: str
    status: str
    mfa_enabled: bool
    is_super_admin: bool
    last_login_at: datetime | None
    locked: bool


@dataclass(frozen=True, slots=True)
class UserList:
    rows: list[UserRow]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class TokenView:
    """What an invitation or reset page may show about its token's owner."""

    full_name: str
    email: str
    expires_at: datetime
    inviter_name: str | None = None
