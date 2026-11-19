import re
import uuid
from dataclasses import dataclass
from datetime import date
from zoneinfo import available_timezones

from pydantic import BaseModel, Field, field_validator


class OrganizationInput(BaseModel):
    legal_name: str = Field(min_length=2, max_length=200)
    trading_name: str | None = Field(default=None, max_length=200)
    rc_number: str | None = Field(default=None, max_length=30)
    tin: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=1000)
    timezone: str = "Africa/Lagos"
    base_currency: str = Field(default="NGN", min_length=3, max_length=3)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value not in available_timezones():
            raise ValueError("Choose a timezone from the list.")
        return value

    @field_validator("trading_name", "rc_number", "tin", "address")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


@dataclass(frozen=True, slots=True)
class OrganizationView:
    legal_name: str
    trading_name: str | None
    rc_number: str | None
    tin: str | None
    address: str | None
    timezone: str
    base_currency: str
    fiscal_year_start_month: int
    brand_primary: str | None = None
    brand_accent: str | None = None

    @property
    def display_name(self) -> str:
        return self.trading_name or self.legal_name


# ------------------------------------------------------------------ structure (Phase 4)


@dataclass(frozen=True, slots=True)
class ReferenceItem:
    """A branch, cost centre or grade."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    rank: int | None = None  # grades only
    address: str | None = None  # branches only
    state: str | None = None  # branches only
    usage: int = 0  # positions or departments using it


@dataclass(frozen=True, slots=True)
class DepartmentRow:
    id: uuid.UUID
    code: str
    name: str
    parent_id: uuid.UUID | None
    depth: int
    cost_centre: str | None
    cost_centre_id: uuid.UUID | None
    is_active: bool
    positions: int
    people: int
    has_children: bool


@dataclass(frozen=True, slots=True)
class HolderView:
    assignment_id: uuid.UUID
    employee_id: uuid.UUID
    kind: str
    start_date: date
    end_date: date | None
    reason: str
    status: str  # current | upcoming | ended


@dataclass(frozen=True, slots=True)
class PositionNode:
    id: uuid.UUID
    title: str
    parent_id: uuid.UUID | None
    depth: int
    department_id: uuid.UUID
    department: str
    grade: str | None
    branch: str | None
    is_active: bool
    holder: HolderView | None  # the effective holder today
    direct_reports: int
    team_size: int
    child_count: int


@dataclass(frozen=True, slots=True)
class PositionDetail:
    node: PositionNode
    grade_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    parent_title: str | None
    history: list[HolderView]
    children: list[PositionNode]
    chain: list[tuple[str, uuid.UUID | None]]  # (position title, holder) upwards


def _hex(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Use a colour like #5A4BDB.")
    return value.upper()


class BrandingInput(BaseModel):
    brand_primary: str | None = None
    brand_accent: str | None = None

    @field_validator("brand_primary", "brand_accent")
    @classmethod
    def _colour(cls, value: str | None) -> str | None:
        return _hex(value)
