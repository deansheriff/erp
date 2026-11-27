"""Support (ADR 0032): teams with a lead and agents, categories routed to a team,
tickets with public replies and internal notes, attachments, business-hours SLA targets
with breach tracking, resolution, ratings and closure."""

import uuid
from datetime import datetime, time
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("open", "in_progress", "waiting", "resolved", "closed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SupportTeam(IdMixin, Base):
    __tablename__ = "support_teams"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    lead_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class TeamAgent(IdMixin, Base):
    __tablename__ = "support_team_agents"
    __table_args__ = (UniqueConstraint("team_id", "employee_id"),)

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_teams.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)


class TicketCategory(IdMixin, Base):
    __tablename__ = "support_categories"
    __table_args__ = (UniqueConstraint("team_id", "name"),)

    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class SlaTarget(IdMixin, Base):
    """Business minutes to first response and to resolution, by priority (confirm)."""

    __tablename__ = "support_sla_targets"
    __table_args__ = (
        CheckConstraint(_in("priority", PRIORITIES), name="priority"),
        CheckConstraint(
            "response_minutes > 0 AND resolve_minutes >= response_minutes", name="minutes"
        ),
    )

    priority: Mapped[str] = mapped_column(String(6), unique=True)
    response_minutes: Mapped[int] = mapped_column(Integer)
    resolve_minutes: Mapped[int] = mapped_column(Integer)


class SupportSettings(Base):
    """One row (id 1): business hours, the warning level and when resolved tickets close."""

    __tablename__ = "support_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="one_row"),
        CheckConstraint("day_end > day_start", name="hours"),
        CheckConstraint("warn_pct BETWEEN 1 AND 99", name="warn"),
        CheckConstraint("close_after_days BETWEEN 1 AND 30", name="close"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    day_start: Mapped[time] = mapped_column(server_default="08:00")
    day_end: Mapped[time] = mapped_column(server_default="17:00")
    warn_pct: Mapped[int] = mapped_column(SmallInteger, server_default="75")
    close_after_days: Mapped[int] = mapped_column(SmallInteger, server_default="3")


class Ticket(IdMixin, Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        CheckConstraint(_in("priority", PRIORITIES), name="priority"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint(
            "(requester_employee_id IS NULL) <> (customer_id IS NULL)", name="requester"
        ),
    )

    number: Mapped[str] = mapped_column(String(20), unique=True)
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_categories.id"))
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_teams.id"), index=True)
    priority: Mapped[str] = mapped_column(String(6))
    status: Mapped[str] = mapped_column(String(11), server_default="open", index=True)
    requester_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    assignee_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # SLA: due times in real time, worked out in business minutes; paused time added on.
    response_due_at: Mapped[datetime]
    resolve_due_at: Mapped[datetime]
    first_response_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]
    paused_minutes: Mapped[int] = mapped_column(Integer, server_default="0")
    response_breached: Mapped[bool] = mapped_column(Boolean, server_default=false())
    resolve_breached: Mapped[bool] = mapped_column(Boolean, server_default=false())
    warned: Mapped[bool] = mapped_column(Boolean, server_default=false())
    breach_alerted: Mapped[bool] = mapped_column(Boolean, server_default=false())
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolution: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None]
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    rating_comment: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


Ticket.__authz__ = AuthzColumns(owner_employee=Ticket.requester_employee_id)


class TicketComment(IdMixin, Base):
    """A public reply or an internal note, or a system line (status, assignment)."""

    __tablename__ = "support_comments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True
    )
    author: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    internal: Mapped[bool] = mapped_column(Boolean, server_default=false())
    system: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketAttachment(IdMixin, Base):
    __tablename__ = "support_attachments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"))
    name: Mapped[str] = mapped_column(String(255))
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())
