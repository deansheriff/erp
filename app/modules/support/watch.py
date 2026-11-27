"""The SLA watch (ADR 0032), run every 15 minutes: marks breaches, warns the assignee (or
the team lead) once at the warning level, emails the team lead once on a resolution
breach, and closes tickets resolved more than the set business days ago."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.identity.models import User
from app.mail import service as mail
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.support import setup, sla, tickets
from app.modules.support.models import SupportTeam, Ticket, TicketComment


@dataclass(frozen=True, slots=True)
class Watched:
    warned: int
    breached: int
    closed: int


def _email_of(db: Session, employee_id: uuid.UUID | None) -> tuple[str, str] | None:
    if employee_id is None:
        return None
    account = people.accounts_of(db, {employee_id}).get(employee_id)
    user = db.get(User, account) if account else None
    if user is None or user.status != "active":
        return None
    return user.email, user.full_name


def _alert(db: Session, row: Ticket, to: tuple[str, str] | None, *, kind: str, left: str) -> None:
    if to is None:
        return
    mail.queue(
        db,
        to=to[0],
        template="support_sla_alert",
        context={
            "organisation": finance.organisation_name(db),
            "name": to[1],
            "number": row.number,
            "title": row.subject,
            "priority": setup.PRIORITY_LABELS[row.priority],
            "kind": kind,  # warning or breached
            "left": left,
        },
    )


def run(db: Session) -> Watched:
    now = clock.now()
    settings = setup.settings(db)
    per_day = setup.day_minutes(db)
    warned = breached = closed = 0
    live = list(
        db.scalars(select(Ticket).where(Ticket.status.in_(("open", "in_progress", "waiting"))))
    )
    for row in live:
        team = db.get(SupportTeam, row.team_id)
        lead = team.lead_employee_id if team else None
        if row.first_response_at is None and now > row.response_due_at:
            row.response_breached = True
        state = tickets.clock_of(db, row)
        if state.state == "breached" and not row.breach_alerted:
            row.resolve_breached = row.breach_alerted = True
            _alert(db, row, _email_of(db, lead), kind="breached", left="")
            breached += 1
        elif state.state == "warning" and not row.warned:
            row.warned = True
            _alert(
                db,
                row,
                _email_of(db, row.assignee_employee_id or lead),
                kind="warning",
                left=sla.describe(state.left, per_day),
            )
            warned += 1
    for row in db.scalars(select(Ticket).where(Ticket.status == "resolved")):
        if row.resolved_at is None:
            continue
        days = tickets.business_minutes(db, row.resolved_at, now) / per_day if per_day else 0
        if days >= settings.close_after_days:
            row.status, row.closed_at, row.paused_at = "closed", now, None
            db.add(TicketComment(ticket_id=row.id, body="Closed automatically.", system=True))
            closed += 1
    db.flush()
    return Watched(warned, breached, closed)
