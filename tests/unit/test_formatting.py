from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.formatting import format_date, format_datetime, format_money


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("1250000"), "₦1,250,000.00"),
        (Decimal("-1250"), "(₦1,250.00)"),
        (Decimal("0"), "₦0.00"),
        (Decimal("0.005"), "₦0.01"),  # half up, not banker's rounding
        (Decimal("-0.004"), "₦0.00"),  # rounds to zero, so no parentheses
    ],
)
def test_money_uses_accounting_format(amount: Decimal, expected: str) -> None:
    assert format_money(amount) == expected


def test_money_shows_other_currencies() -> None:
    assert format_money(Decimal("10"), "USD") == "$10.00"
    assert format_money(Decimal("10"), "XOF") == "XOF 10.00"


def test_money_rejects_floats() -> None:
    with pytest.raises(TypeError):
        format_money(10.5)  # type: ignore[arg-type]


def test_dates_are_day_month_year() -> None:
    assert format_date(date(2026, 11, 7)) == "7 Nov 2026"


def test_datetimes_convert_utc_to_organisation_time() -> None:
    utc = datetime(2026, 11, 17, 13, 5, tzinfo=UTC)
    assert format_datetime(utc, "Africa/Lagos") == "17 Nov 2026, 14:05 WAT"


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_datetime(datetime(2026, 11, 17, 13, 5), "Africa/Lagos")  # noqa: DTZ001
