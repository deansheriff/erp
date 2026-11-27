"""What support records about an agent over a period, as plain facts for the assistant's
appraisal evidence (ADR 0035): tickets resolved, their ratings and comments, and SLA
breaches. The caller has already checked the person is in the reader's scope."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.support.models import Ticket

COMMENTS = 5


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(get_settings().timezone)
    return (
        datetime.combine(start, time.min, tzinfo=zone),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone),
    )


def facts(db: Session, employee_id: uuid.UUID, start: date, end: date) -> list[tuple[str, str]]:
    low, high = _bounds(start, end)
    tickets = list(
        db.scalars(
            select(Ticket)
            .where(
                Ticket.assignee_employee_id == employee_id,
                Ticket.resolved_at >= low,
                Ticket.resolved_at < high,
            )
            .order_by(Ticket.resolved_at)
        )
    )
    if not tickets:
        return []
    out = [("Support", f"Support tickets they resolved: {len(tickets)}.")]
    ratings = [t.rating for t in tickets if t.rating]
    if ratings:
        average = sum(ratings) / len(ratings)
        out.append(
            (
                "Support",
                f"Requesters' satisfaction with those tickets averaged {average:.1f} of 5, "
                f"from {len(ratings)} responses.",
            )
        )
    breached = sum(1 for t in tickets if t.response_breached or t.resolve_breached)
    out.append(("Support", f"Of those tickets, {breached} missed a response or resolution target."))
    for t in [t for t in tickets if t.rating_comment][-COMMENTS:]:
        comment = " ".join((t.rating_comment or "").split())[:300]
        out.append(("Support", f'Requester comment on {t.number}: "{comment}"'))
    return out
