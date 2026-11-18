"""Turns pydantic validation errors into plain-language messages for forms.

Library wording ("value is not a valid email address: The part after the @-sign…")
never reaches the page; our own validators already raise friendly messages.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import ValidationError

from app.core.config import get_settings


def _message(err: dict[str, Any]) -> str:  # noqa: PLR0911  (one plain answer per error kind)
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    raw = str(err.get("msg", ""))
    if kind == "missing":
        return "This is required."
    if kind == "string_too_short":
        minimum = ctx.get("min_length", 1)
        return "This is required." if minimum == 1 else f"Enter at least {minimum} characters."
    if kind == "string_too_long":
        return f"Use no more than {ctx.get('max_length')} characters."
    if kind in {"int_parsing", "int_type", "int_from_float"}:
        return "Enter a whole number."
    if kind in {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}:
        return "Enter a value within the allowed range."
    if kind == "literal_error":
        return "Choose one of the options."
    if "email address" in raw:
        return "Enter a valid work email address, like name@company.ng."
    if kind == "value_error" and raw.startswith("Value error, "):
        return raw.removeprefix("Value error, ")
    return "Check this value."


def field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ("form",)
        errors.setdefault(str(loc[-1]), _message(dict(err)))
    return errors


async def all_fields(request: Request) -> dict[str, str]:
    """Every text field of a form, for forms whose field names are data (a permission
    matrix). Used as a dependency so routes stay synchronous; Starlette caches the body."""
    form = await request.form()
    return {k: v for k, v in form.multi_items() if isinstance(v, str)}


def parse_day(value: str | None) -> date | None:
    """An `<input type="date">` value, or None when blank or malformed."""
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def start_of_day(day: date) -> datetime:
    """Midnight at the start of `day` in the organisation's timezone, as UTC."""
    local = datetime.combine(day, time.min, tzinfo=ZoneInfo(get_settings().timezone))
    return local.astimezone(UTC)


def end_of_day(day: date) -> datetime:
    """The end of `day` (exclusive), so a period ending on the 30th includes the 30th."""
    return start_of_day(day + timedelta(days=1))
