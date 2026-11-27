"""What other modules may use from support: facts about an agent's tickets (appraisal
evidence) and a ticket's brief for an agent's assistant suggestions (ADR 0035)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.support import tickets
from app.modules.support.evidence import facts as evidence_facts
from app.modules.support.models import Ticket, TicketCategory, TicketComment


@dataclass(frozen=True, slots=True)
class TicketBrief:
    number: str
    subject: str
    description: str
    category: str
    open: bool
    replies: list[tuple[str, str]]  # (who, text): public replies only, oldest first
    categories: list[str]  # active category names


def ticket_brief(db: Session, actor: Actor, ticket_id: uuid.UUID) -> TicketBrief | None:
    """None unless the actor is an agent for this ticket."""
    row = db.get(Ticket, ticket_id)
    if row is None or not tickets.is_agent_for(db, actor, row):
        return None
    comments = list(
        db.scalars(
            select(TicketComment)
            .where(
                TicketComment.ticket_id == row.id,
                TicketComment.internal.is_(False),
                TicketComment.system.is_(False),
            )
            .order_by(TicketComment.created_at)
        )
    )
    names = dict(
        db.execute(
            select(User.id, User.full_name).where(
                User.id.in_({c.author for c in comments if c.author})
            )
        ).all()
    )
    category = db.get(TicketCategory, row.category_id)
    return TicketBrief(
        row.number,
        row.subject,
        row.description,
        category.name if category else "",
        row.status not in ("resolved", "closed"),
        [(names.get(c.author, "Someone") if c.author else "Someone", c.body) for c in comments],
        list(
            db.scalars(
                select(TicketCategory.name)
                .where(TicketCategory.is_active)
                .order_by(TicketCategory.name)
            )
        ),
    )


__all__ = ["TicketBrief", "evidence_facts", "ticket_brief"]
