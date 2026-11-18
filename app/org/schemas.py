from dataclasses import dataclass
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

    @property
    def display_name(self) -> str:
        return self.trading_name or self.legal_name
