"""Turns pydantic validation errors into plain-language messages for forms.

Library wording ("value is not a valid email address: The part after the @-sign…")
never reaches the page; our own validators already raise friendly messages.
"""

from typing import Any

from pydantic import ValidationError


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
