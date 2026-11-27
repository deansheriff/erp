"""Business-hours arithmetic for SLA targets (ADR 0032): Monday to Friday within the day's
hours in the organisation's timezone, skipping public holidays from Leave."""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

Holidays = Callable[[date, date], set[date]]
MAX_DAYS = 3660  # ten years: a guard against bad settings


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _working(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def add_minutes(
    start: datetime, minutes: int, *, day_start: time, day_end: time, holidays: Holidays
) -> datetime:
    """The moment `minutes` business minutes after `start` (both aware, returned in UTC)."""
    zone = _zone()
    moment = start.astimezone(zone)
    found = holidays(moment.date(), moment.date() + timedelta(days=minutes // 60 + 60))
    left = max(minutes, 0)
    for _ in range(MAX_DAYS):
        day = moment.date()
        opens = datetime.combine(day, day_start, zone)
        closes = datetime.combine(day, day_end, zone)
        if _working(day, found) and moment < closes:
            moment = max(moment, opens)
            available = int((closes - moment).total_seconds() // 60)
            if left <= available:
                return (moment + timedelta(minutes=left)).astimezone(start.tzinfo)
            left -= available
        moment = datetime.combine(day + timedelta(days=1), day_start, zone)
    raise ValueError("The SLA target is too long to work out.")


def minutes_between(
    start: datetime, end: datetime, *, day_start: time, day_end: time, holidays: Holidays
) -> int:
    """Business minutes from `start` to `end` (0 if end is not after start)."""
    if end <= start:
        return 0
    zone = _zone()
    first, last = start.astimezone(zone), end.astimezone(zone)
    found = holidays(first.date(), last.date())
    total = 0.0
    day = first.date()
    while day <= last.date():
        if _working(day, found):
            opens = max(datetime.combine(day, day_start, zone), first)
            closes = min(datetime.combine(day, day_end, zone), last)
            if closes > opens:
                total += (closes - opens).total_seconds() / 60
        day += timedelta(days=1)
    return int(total)


def describe(minutes: int, per_day: int) -> str:
    """Business minutes as "2h 15m", or in days when they're whole days ("3 days")."""
    if per_day and minutes >= per_day and minutes % per_day == 0:
        days = minutes // per_day
        return f"{days} day{'s' if days != 1 else ''}"
    hours, rest = divmod(abs(minutes), 60)
    text = f"{hours}h {rest}m" if hours and rest else (f"{hours}h" if hours else f"{rest}m")
    return f"-{text}" if minutes < 0 else text
