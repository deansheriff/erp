"""Support tickets (ADR 0032). An employee raises one for themself, or an agent records
one for a customer. It lands unassigned in its category's team queue; an agent takes it,
or the team lead assigns it. Public replies go to the requester (emailed), internal
notes stay with agents. Waiting on the requester pauses the resolution clock. Resolving
needs a note; the requester can reopen or rate it until it closes on its own."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.mail import service as mail
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.support import setup, sla
from app.modules.support.models import (
    PRIORITIES,
    SupportTeam,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketComment,
)

MAX_BODY = 20_000


def _me(db: Session, actor: Actor) -> uuid.UUID | None:
    return people.employee_of_user(db, actor.user_id)


def _locked(db: Session, ticket_id: uuid.UUID) -> Ticket:
    row = db.scalars(select(Ticket).where(Ticket.id == ticket_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def sees_all(db: Session, actor: Actor) -> bool:
    return setup.can(db, actor, setup.MANAGE)


def is_agent_for(db: Session, actor: Actor, row: Ticket) -> bool:
    """An agent on the ticket's team (or someone who manages support)."""
    if not setup.can(db, actor, setup.WORK):
        return False
    return sees_all(db, actor) or row.team_id in setup.teams_of(db, _me(db, actor))


def is_lead_for(db: Session, actor: Actor, row: Ticket) -> bool:
    team = db.get(SupportTeam, row.team_id)
    me = _me(db, actor)
    lead = team is not None and me is not None and team.lead_employee_id == me
    return setup.can(db, actor, setup.WORK) and (lead or sees_all(db, actor))


def is_requester(db: Session, actor: Actor, row: Ticket) -> bool:
    me = _me(db, actor)
    return me is not None and row.requester_employee_id == me


def _visible(db: Session, actor: Actor, row: Ticket) -> None:
    if not (is_requester(db, actor, row) or is_agent_for(db, actor, row)):
        raise NotFound()


# ------------------------------------------------------------------ the SLA clock


def _due(db: Session, start: datetime, minutes: int) -> datetime:
    row = setup.settings(db)
    return sla.add_minutes(
        start,
        minutes,
        day_start=row.day_start,
        day_end=row.day_end,
        holidays=setup.holidays_for(db),
    )


def business_minutes(db: Session, start: datetime, end: datetime) -> int:
    row = setup.settings(db)
    return sla.minutes_between(
        start, end, day_start=row.day_start, day_end=row.day_end, holidays=setup.holidays_for(db)
    )


def _set_dues(db: Session, row: Ticket) -> None:
    target = setup.targets(db)[row.priority]
    row.response_due_at = _due(db, row.created_at, target.response_minutes)
    row.resolve_due_at = _due(db, row.created_at, target.resolve_minutes + row.paused_minutes)


def _pause(row: Ticket) -> None:
    if row.paused_at is None:
        row.paused_at = clock.now()


def _resume(db: Session, row: Ticket) -> None:
    if row.paused_at is not None:
        row.paused_minutes += business_minutes(db, row.paused_at, clock.now())
        row.paused_at = None
        _set_dues(db, row)


def _system(db: Session, row: Ticket, actor: Actor | None, text: str) -> None:
    db.add(
        TicketComment(
            ticket_id=row.id, author=actor.user_id if actor else None, body=text, system=True
        )
    )
    row.updated_at = clock.now()


# ------------------------------------------------------------------ email


def _requester_email(db: Session, row: Ticket) -> tuple[str | None, str]:
    if row.customer_id:
        names = finance.customer_names(db, {row.customer_id})
        return row.contact_email, names.get(row.customer_id, "")
    account = people.accounts_of(db, {row.requester_employee_id}).get(row.requester_employee_id)  # type: ignore[arg-type]
    user = db.get(User, account) if account else None
    return (user.email, user.full_name) if user and user.status == "active" else (None, "")


def _notify(db: Session, row: Ticket, *, kind: str, body: str) -> None:
    to, name = _requester_email(db, row)
    if not to:
        return
    mail.queue(
        db,
        to=to,
        template="support_ticket_update",
        context={
            "organisation": finance.organisation_name(db),
            "name": name,
            "number": row.number,
            "title": row.subject,
            "kind": kind,  # reply or resolved
            "body": body,
        },
    )


# ------------------------------------------------------------------ raising


@dataclass(frozen=True, slots=True)
class TicketInput:
    category_id: str
    priority: str
    subject: str
    description: str
    customer_id: str = ""  # agents recording a customer's ticket
    contact_email: str = ""


def raise_ticket(db: Session, actor: Actor, data: TicketInput) -> uuid.UUID:
    me = _me(db, actor)
    for_customer = bool(data.customer_id)
    if for_customer and not setup.can(db, actor, setup.WORK):
        raise PermissionDenied("Only support agents record customers' tickets.")
    if not for_customer and (me is None or not setup.can(db, actor, setup.CREATE)):
        raise PermissionDenied("Tickets are for people on the staff list.")
    errors: dict[str, str] = {}
    category = db.get(TicketCategory, setup.uuid_or_none(data.category_id) or uuid.uuid4())
    if category is None or category not in setup.categories(db, active_only=True):
        errors["category_id"] = "Choose what it's about."
    if data.priority not in PRIORITIES:
        errors["priority"] = "Choose a priority."
    if not data.subject.strip():
        errors["subject"] = "Give it a short subject."
    if not data.description.strip():
        errors["description"] = "Describe what's wrong or what you need."
    customer = setup.uuid_or_none(data.customer_id)
    contact = data.contact_email.strip()
    if for_customer:
        known = dict(finance.customer_choices(db))
        if customer not in known:
            errors["customer_id"] = "Choose the customer."
        if contact and "@" not in contact:
            errors["contact_email"] = "Enter an email address, or leave it blank."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert category is not None  # noqa: S101
    row = Ticket(
        number=numbering.next_number(db, "support_ticket"),
        subject=data.subject.strip()[:200],
        description=data.description.strip()[:MAX_BODY],
        category_id=category.id,
        team_id=category.team_id,
        priority=data.priority,
        requester_employee_id=None if for_customer else me,
        customer_id=customer if for_customer else None,
        contact_email=(contact or _customer_email(db, customer)) if for_customer else None,
        created_by=actor.user_id,
        created_at=clock.now(),
    )
    row.response_due_at = row.resolve_due_at = row.created_at  # set properly below
    db.add(row)
    db.flush()
    _set_dues(db, row)
    db.flush()
    record_by(
        db, actor, "support.ticket.raise", entity_type="support_ticket", entity_id=str(row.id)
    )
    return row.id


def _customer_email(db: Session, customer_id: uuid.UUID | None) -> str | None:
    if customer_id is None:
        return None
    return finance.customer_email(db, customer_id)


# ------------------------------------------------------------------ working


def _work(db: Session, actor: Actor, ticket_id: uuid.UUID) -> Ticket:
    row = _locked(db, ticket_id)
    if not is_agent_for(db, actor, row):
        raise PermissionDenied("Only agents on this ticket's team work it.")
    if row.status == "closed":
        raise Conflict("This ticket is closed.")
    return row


def take(db: Session, actor: Actor, ticket_id: uuid.UUID) -> None:
    row = _work(db, actor, ticket_id)
    me = _me(db, actor)
    if me is None:
        raise PermissionDenied("Taking tickets is for people on the staff list.")
    row.assignee_employee_id = me
    if row.status == "open":
        row.status = "in_progress"
    _system(db, row, actor, "Took this ticket.")
    db.flush()


def assign(db: Session, actor: Actor, ticket_id: uuid.UUID, employee_id: str) -> None:
    row = _work(db, actor, ticket_id)
    if not is_lead_for(db, actor, row):
        raise PermissionDenied("Only the team lead assigns tickets.")
    target = setup.uuid_or_none(employee_id)
    team = setup.agents_of(db, row.team_id)
    lead = db.get(SupportTeam, row.team_id)
    if lead and lead.lead_employee_id:
        team.add(lead.lead_employee_id)
    if target not in team:
        raise ValidationFailed(field_errors={"assignee_id": "Choose an agent on the team."})
    row.assignee_employee_id = target
    if row.status == "open":
        row.status = "in_progress"
    name = people.names(db, {target}).get(target, "")
    _system(db, row, actor, f"Assigned to {name}.")
    db.flush()


def set_priority(db: Session, actor: Actor, ticket_id: uuid.UUID, priority: str) -> None:
    row = _work(db, actor, ticket_id)
    if priority not in PRIORITIES:
        raise ValidationFailed(field_errors={"priority": "Choose a priority."})
    if priority != row.priority:
        before = row.priority
        row.priority = priority
        _set_dues(db, row)
        _system(
            db,
            row,
            actor,
            f"Priority changed from {setup.PRIORITY_LABELS[before]} to "
            f"{setup.PRIORITY_LABELS[priority]}.",
        )
        db.flush()


def reply(
    db: Session,
    actor: Actor,
    ticket_id: uuid.UUID,
    *,
    body: str,
    internal: bool,
    then: str = "",
) -> None:
    """A public reply or internal note. `then` may be "waiting" (agents)."""
    row = _locked(db, ticket_id)
    agent = is_agent_for(db, actor, row)
    requester = is_requester(db, actor, row)
    if not (agent or requester):
        raise NotFound()
    if row.status == "closed":
        raise Conflict("This ticket is closed. Raise a new one.")
    if internal and not agent:
        raise PermissionDenied("Internal notes are for agents.")
    if not body.strip():
        raise ValidationFailed(field_errors={"body": "Write something first."})
    db.add(
        TicketComment(
            ticket_id=row.id, author=actor.user_id, body=body.strip()[:MAX_BODY], internal=internal
        )
    )
    row.updated_at = clock.now()
    if agent and not internal:
        if row.first_response_at is None:
            row.first_response_at = clock.now()
        _notify(db, row, kind="reply", body=body.strip())
    if requester and not agent and row.status == "waiting":
        _resume(db, row)
        row.status = "in_progress"
    if agent and then == "waiting" and row.status in ("open", "in_progress"):
        _pause(row)
        row.status = "waiting"
    elif agent and row.status == "open":
        row.status = "in_progress"
    db.flush()


def resume(db: Session, actor: Actor, ticket_id: uuid.UUID) -> None:
    row = _work(db, actor, ticket_id)
    if row.status != "waiting":
        raise Conflict("This ticket isn't waiting on the requester.")
    _resume(db, row)
    row.status = "in_progress"
    _system(db, row, actor, "Back in progress.")
    db.flush()


def resolve(db: Session, actor: Actor, ticket_id: uuid.UUID, *, resolution: str) -> None:
    row = _work(db, actor, ticket_id)
    if row.status == "resolved":
        raise Conflict("This ticket is already resolved.")
    if not resolution.strip():
        raise ValidationFailed(field_errors={"resolution": "Say how it was resolved."})
    now = clock.now()
    if row.paused_at is not None:
        _resume(db, row)
    if row.first_response_at is None:
        row.first_response_at = now
    row.response_breached = row.response_breached or row.first_response_at > row.response_due_at
    row.resolve_breached = row.resolve_breached or now > row.resolve_due_at
    row.status, row.resolved_at, row.resolved_by = "resolved", now, actor.user_id
    row.resolution = resolution.strip()[:MAX_BODY]
    _pause(row)  # time while resolved doesn't count if it's reopened
    _system(db, row, actor, "Resolved.")
    _notify(db, row, kind="resolved", body=row.resolution)
    db.flush()
    record_by(
        db, actor, "support.ticket.resolve", entity_type="support_ticket", entity_id=str(row.id)
    )


def reopen(db: Session, actor: Actor, ticket_id: uuid.UUID) -> None:
    row = _locked(db, ticket_id)
    if not (is_requester(db, actor, row) or is_agent_for(db, actor, row)):
        raise NotFound()
    if row.status != "resolved":
        raise Conflict("Only a resolved ticket can be reopened.")
    _resume(db, row)
    row.status, row.resolved_at, row.resolved_by = "in_progress", None, None
    row.rating = row.rating_comment = None
    _system(db, row, actor, "Reopened.")
    db.flush()


def rate(db: Session, actor: Actor, ticket_id: uuid.UUID, *, score: str, comment: str) -> None:
    row = _locked(db, ticket_id)
    if not is_requester(db, actor, row):
        raise PermissionDenied("Only the person who raised it rates it.")
    if row.status not in ("resolved", "closed"):
        raise Conflict("Rate a ticket once it's resolved.")
    try:
        value = int(score)
    except ValueError:
        value = 0
    if not 1 <= value <= 5:
        raise ValidationFailed(field_errors={"score": "Choose 1 to 5 stars."})
    row.rating, row.rating_comment = value, comment.strip()[:2000] or None
    db.flush()


def attach(
    db: Session,
    actor: Actor,
    ticket_id: uuid.UUID,
    *,
    filename: str | None,
    mime: str | None,
    data: bytes,
) -> None:
    row = _locked(db, ticket_id)
    _visible(db, actor, row)
    if row.status == "closed":
        raise Conflict("This ticket is closed.")
    file_id = files.upload(
        db,
        actor,
        purpose_key="support_attachment",
        entity_id=str(row.id),
        filename=filename,
        declared_mime=mime,
        data=data,
    )
    db.add(
        TicketAttachment(
            ticket_id=row.id,
            file_id=file_id,
            name=files.sanitise_filename(filename),
            added_by=actor.user_id,
        )
    )
    row.updated_at = clock.now()
    db.flush()


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class Clock:
    """Where a ticket stands against its resolution target, in business minutes."""

    state: str  # met, breached, running, warning, paused, done
    left: int  # business minutes to the target (negative once breached)
    used_pct: int


def clock_of(db: Session, row: Ticket) -> Clock:
    target = setup.targets(db)[row.priority].resolve_minutes
    now = clock.now()
    if row.status in ("resolved", "closed"):
        return Clock("breached" if row.resolve_breached else "met", 0, 100)
    elapsed = business_minutes(db, row.created_at, row.paused_at or now) - row.paused_minutes
    used = int(elapsed * 100 / target) if target else 0
    left = target - elapsed
    if row.paused_at is not None:
        return Clock("paused", left, used)
    if left < 0 or now > row.resolve_due_at:
        return Clock("breached", left, used)
    return Clock("warning" if used >= setup.settings(db).warn_pct else "running", left, used)


@dataclass(frozen=True, slots=True)
class TicketRow:
    ticket: Ticket
    who: str  # requester or customer
    category: str
    team: str
    assignee: str | None
    clock: Clock


TABS = ("mine", "queue", "assigned", "all")


def list_tickets(
    db: Session, actor: Actor, tab: str, *, team: str = "", status: str = ""
) -> list[TicketRow]:
    me = _me(db, actor)
    query = select(Ticket).order_by(Ticket.updated_at.desc())
    if tab == "mine":
        if me is None:
            return []
        query = query.where(Ticket.requester_employee_id == me)
    else:
        if not setup.can(db, actor, setup.WORK):
            return []
        if not sees_all(db, actor):
            query = query.where(Ticket.team_id.in_(setup.teams_of(db, me) or {uuid.uuid4()}))
        if tab == "queue":
            query = query.where(
                Ticket.assignee_employee_id.is_(None), Ticket.status.in_(("open", "in_progress"))
            )
        elif tab == "assigned":
            query = query.where(
                Ticket.assignee_employee_id == me, Ticket.status.not_in(("resolved", "closed"))
            )
    if team:
        query = query.where(Ticket.team_id == setup.uuid_or_none(team))
    if status:
        query = query.where(Ticket.status == status)
    elif tab != "all":
        query = query.where(or_(Ticket.status != "closed", Ticket.requester_employee_id == me))
    rows = list(db.scalars(query.limit(300)))
    return _rows(db, rows)


def _rows(db: Session, rows: list[Ticket]) -> list[TicketRow]:
    names = people.names(
        db,
        {r.requester_employee_id for r in rows if r.requester_employee_id}
        | {r.assignee_employee_id for r in rows if r.assignee_employee_id},
    )
    customers = finance.customer_names(db, {r.customer_id for r in rows if r.customer_id})
    categories = {c.id: c.name for c in setup.categories(db)}
    teams = {t.id: t.name for t in setup.teams(db)}
    return [
        TicketRow(
            r,
            customers.get(r.customer_id, "")
            if r.customer_id
            else names.get(r.requester_employee_id, ""),  # type: ignore[arg-type]
            categories.get(r.category_id, ""),
            teams.get(r.team_id, ""),
            names.get(r.assignee_employee_id) if r.assignee_employee_id else None,
            clock_of(db, r),
        )
        for r in rows
    ]


def counts(db: Session, actor: Actor) -> dict[str, int]:
    if not setup.can(db, actor, setup.WORK):
        return {}
    return {
        "queue": len(list_tickets(db, actor, "queue")),
        "assigned": len(list_tickets(db, actor, "assigned")),
    }


@dataclass(frozen=True, slots=True)
class Entry:
    comment: TicketComment
    author: str | None


@dataclass(frozen=True, slots=True)
class TicketView:
    row: TicketRow
    entries: list[Entry]
    attachments: list[TicketAttachment]
    agents: list[tuple[uuid.UUID, str]]  # who the lead can assign to
    response: str  # met, breached, waiting
    target_response: int
    target_resolve: int
    per_day: int
    is_agent: bool
    is_lead: bool
    is_requester: bool
    customer_email: str | None


def get(db: Session, actor: Actor, ticket_id: uuid.UUID) -> TicketView:
    row = db.get(Ticket, ticket_id)
    if row is None:
        raise NotFound()
    _visible(db, actor, row)
    agent = is_agent_for(db, actor, row)
    comments = [
        c
        for c in db.scalars(
            select(TicketComment)
            .where(TicketComment.ticket_id == row.id)
            .order_by(TicketComment.created_at)
        )
        if agent or not c.internal
    ]
    authors = setup.user_names(db, {c.author for c in comments})
    team = db.get(SupportTeam, row.team_id)
    pool = setup.agents_of(db, row.team_id) | (
        {team.lead_employee_id} if team and team.lead_employee_id else set()
    )
    names = people.names(db, pool)
    target = setup.targets(db)[row.priority]
    now = clock.now()
    if row.first_response_at is not None:
        response = "met" if row.first_response_at <= row.response_due_at else "breached"
    else:
        response = "breached" if now > row.response_due_at else "waiting"
    return TicketView(
        _rows(db, [row])[0],
        [Entry(c, authors.get(c.author) if c.author else None) for c in comments],
        list(
            db.scalars(
                select(TicketAttachment)
                .where(TicketAttachment.ticket_id == row.id)
                .order_by(TicketAttachment.added_at)
            )
        ),
        sorted(((e, names.get(e, "")) for e in pool), key=lambda p: p[1]),
        response,
        target.response_minutes,
        target.resolve_minutes,
        setup.day_minutes(db),
        agent,
        is_lead_for(db, actor, row),
        is_requester(db, actor, row),
        row.contact_email,
    )
