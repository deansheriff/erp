"""The one place the application reads the current time (UTC). Tests may patch `now`."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    """Today's date in the organisation's timezone (dates such as position assignments
    and leave are calendar dates there, not in UTC)."""
    return now().astimezone(ZoneInfo(get_settings().timezone)).date()


def local_date(moment: datetime) -> date:
    """The calendar date of `moment` in the organisation's timezone."""
    return moment.astimezone(ZoneInfo(get_settings().timezone)).date()
