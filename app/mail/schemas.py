from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class MailSettingsInput(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"]
    username: str | None = Field(default=None, max_length=255)
    # None or empty means "keep the saved password".
    password: str | None = Field(default=None, max_length=500)
    from_name: str = Field(min_length=1, max_length=200)
    from_address: EmailStr
    reply_to: EmailStr | None = None


@dataclass(frozen=True, slots=True)
class MailSettingsView:
    host: str
    port: int
    security: str
    username: str | None
    has_password: bool
    from_name: str
    from_address: str
    reply_to: str | None
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_test_to: str | None


@dataclass(frozen=True, slots=True)
class OutboxStats:
    sent_today: int
    waiting: int
    failed: int


@dataclass(frozen=True, slots=True)
class TestResult:
    ok: bool
    message: str
