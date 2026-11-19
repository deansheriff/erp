"""Display formatting for money and dates (brief section 8)."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

CURRENCY_SYMBOLS = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")


def format_money(amount: Decimal, currency: str = "NGN") -> str:
    """Accounting format: `₦1,250,000.00`, negatives as `(₦1,250.00)`."""
    if not isinstance(amount, Decimal):
        raise TypeError("Money must be a Decimal")
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    body = f"{symbol}{abs(rounded):,.2f}"
    return f"({body})" if rounded < 0 else body


def format_date(value: date) -> str:
    """`17 Nov 2026`."""
    return f"{value.day} {value:%b %Y}"


def format_datetime(value: datetime, tz_name: str) -> str:
    """`17 Nov 2026, 14:05 WAT`, converted from UTC to the organisation's timezone."""
    if value.tzinfo is None:
        raise ValueError("Datetimes must be timezone-aware (stored in UTC)")
    local = value.astimezone(ZoneInfo(tz_name))
    return f"{format_date(local.date())}, {local:%H:%M} {local.tzname()}"


def format_size(size: int) -> str:
    """`248 KB`, `2 MB`, `12.4 GB` (binary units, as file managers show them)."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024
    return f"{value:.1f} TB"
