"""Counting leave days (ADR 0018): working days skip weekends and public holidays, and a
request may start in the afternoon or end at midday; calendar-day types count every day."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.modules.leave.models import PublicHoliday

WEEKEND = frozenset({5, 6})  # Saturday, Sunday (confirm)
HALF = Decimal("0.5")
MAX_SPAN_DAYS = 366


def holidays_between(db: Session, start: date, end: date) -> dict[date, str]:
    rows = db.execute(
        select(PublicHoliday.day, PublicHoliday.name).where(
            PublicHoliday.day >= start, PublicHoliday.day <= end
        )
    ).all()
    return dict(rows)


def is_working_day(day: date, holidays: dict[date, str]) -> bool:
    return day.weekday() not in WEEKEND and day not in holidays


def count(
    db: Session,
    start: date,
    end: date,
    *,
    counting: str,
    start_half: bool = False,
    end_half: bool = False,
) -> Decimal:
    """Days a request uses. Raises ValidationFailed with a field message when the dates
    don't make a request."""
    if end < start:
        raise ValidationFailed(field_errors={"end_date": "The end can't be before the start."})
    if (end - start).days >= MAX_SPAN_DAYS:
        raise ValidationFailed(field_errors={"end_date": "Keep a request within a year."})
    if start.year != end.year:
        raise ValidationFailed(
            field_errors={"end_date": "Split leave that crosses the new year into two requests."}
        )
    if counting == "calendar":
        if start_half or end_half:
            raise ValidationFailed(
                field_errors={"start_half": "This type of leave is taken in whole days."}
            )
        return Decimal((end - start).days + 1)
    if start == end and start_half and end_half:
        raise ValidationFailed(
            field_errors={"start_half": "Choose a morning or an afternoon, not both."}
        )
    holidays = holidays_between(db, start, end)
    total = Decimal(0)
    day = start
    while day <= end:
        if is_working_day(day, holidays):
            total += 1
        day += timedelta(days=1)
    if start_half and is_working_day(start, holidays):
        total -= HALF
    if end_half and is_working_day(end, holidays):
        total -= HALF
    if total <= 0:
        raise ValidationFailed(
            field_errors={"end_date": "There are no working days in these dates."}
        )
    return total
