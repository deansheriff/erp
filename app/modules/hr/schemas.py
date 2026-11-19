import uuid
from dataclasses import dataclass

from pydantic import BaseModel, EmailStr, Field, field_validator


class PersonInput(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    other_names: str | None = Field(default=None, max_length=120)
    work_email: EmailStr | None = None
    user_id: uuid.UUID | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This is required.")
        return value

    @field_validator("other_names")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return " ".join(value.split()) or None if value else None


@dataclass(frozen=True, slots=True)
class PersonRow:
    """Directory fields only (field class `directory`)."""

    id: uuid.UUID
    full_name: str
    first_name: str
    last_name: str
    work_email: str | None
    status: str
    has_account: bool
    positions: list[tuple[str, str]]  # (title, kind), best kind first

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + self.last_name[:1]).upper()
