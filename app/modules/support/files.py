"""File purposes for support (ADR 0032): files attached to a ticket. The requester and
the agents who may work it can see them; the service checks who may attach."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.support.models import Ticket


def _ticket(db: Session, entity_id: str) -> Resource | None:
    try:
        ticket = db.get(Ticket, uuid.UUID(entity_id))
    except ValueError:
        return None
    if ticket is None:
        return None
    return Resource(owner_employee_id=ticket.requester_employee_id)


SUPPORT_ATTACHMENT = FilePurpose(
    key="support_attachment",
    label="Ticket attachment",
    entity_type="support_ticket",
    allowed_types=frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"}),
    max_bytes=10 * MB,
    attach_permission="support.ticket.read",
    view_permission="support.ticket.read",
    # Kept two years after upload (confirm).
    retention=timedelta(days=2 * 365),
    inline=True,
    classification="directory",
    resolve=_ticket,
)

PURPOSES = (SUPPORT_ATTACHMENT,)
