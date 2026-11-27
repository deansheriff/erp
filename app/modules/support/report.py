"""The SLA report (ADR 0032): tickets resolved in a month, by team and priority and by
agent: first response and resolution met, breaches, average resolution time in business
hours (time waiting on the requester left out) and average rating."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.support import setup, tickets
from app.modules.support.models import PRIORITIES, Ticket


@dataclass(slots=True)
class Tally:
    tickets: int = 0
    response_met: int = 0
    resolve_met: int = 0
    minutes: int = 0  # business minutes to resolve, summed
    ratings: list[int] = field(default_factory=list)

    @property
    def breached(self) -> int:
        return self.tickets - self.resolve_met

    def pct(self, part: int) -> int | None:
        return round(part * 100 / self.tickets) if self.tickets else None

    @property
    def average_hours(self) -> Decimal | None:
        if not self.tickets:
            return None
        return (Decimal(self.minutes) / self.tickets / 60).quantize(Decimal("0.1"))

    @property
    def rating(self) -> Decimal | None:
        if not self.ratings:
            return None
        return (Decimal(sum(self.ratings)) / len(self.ratings)).quantize(Decimal("0.1"))


@dataclass(frozen=True, slots=True)
class Report:
    month: date
    total: Tally
    by_team: list[tuple[str, str, Tally]]  # team, priority, tally
    by_agent: list[tuple[str, Tally]]


def _bounds(month: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(get_settings().timezone)
    start = datetime.combine(month.replace(day=1), time(0), zone)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, datetime.combine(following, time(0), zone)


def build(db: Session, actor: Actor, month: date, *, team: str = "") -> Report:
    setup.require(db, actor, setup.REPORT)
    start, end = _bounds(month)
    query = select(Ticket).where(Ticket.resolved_at >= start, Ticket.resolved_at < end)
    team_id = setup.uuid_or_none(team)
    if team_id:
        query = query.where(Ticket.team_id == team_id)
    rows = list(db.scalars(query))
    teams = {t.id: t.name for t in setup.teams(db)}
    names = people.names(db, {r.assignee_employee_id for r in rows if r.assignee_employee_id})
    total = Tally()
    by_team: dict[tuple[uuid.UUID, str], Tally] = defaultdict(Tally)
    by_agent: dict[uuid.UUID | None, Tally] = defaultdict(Tally)
    for row in rows:
        assert row.resolved_at is not None  # noqa: S101  (filtered above)
        response_ok = (
            row.first_response_at is not None and row.first_response_at <= row.response_due_at
        )
        resolve_ok = not row.resolve_breached
        minutes = max(
            tickets.business_minutes(db, row.created_at, row.resolved_at) - row.paused_minutes, 0
        )
        for tally in (
            total,
            by_team[(row.team_id, row.priority)],
            by_agent[row.assignee_employee_id],
        ):
            tally.tickets += 1
            tally.response_met += response_ok
            tally.resolve_met += resolve_ok
            tally.minutes += minutes
            if row.rating:
                tally.ratings.append(row.rating)
    order = {p: i for i, p in enumerate(reversed(PRIORITIES))}
    return Report(
        month.replace(day=1),
        total,
        [
            (teams.get(t, ""), p, tally)
            for (t, p), tally in sorted(
                by_team.items(), key=lambda kv: (teams.get(kv[0][0], ""), order[kv[0][1]])
            )
        ],
        sorted(
            ((names.get(a, "") if a else "Unassigned", tally) for a, tally in by_agent.items()),
            key=lambda kv: (-kv[1].tickets, kv[0]),
        ),
    )
