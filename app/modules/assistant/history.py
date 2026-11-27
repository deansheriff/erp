"""A person's assistant history (ADR 0035): their own requests only, deleted after the
history period by a nightly task."""

import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import NotFound
from app.identity.schemas import Actor
from app.modules.assistant import common
from app.modules.assistant.models import AssistantRequest


def mine(
    db: Session, actor: Actor, *, feature: str = "", limit: int = 50
) -> list[AssistantRequest]:
    query = (
        select(AssistantRequest)
        .where(AssistantRequest.user_id == actor.user_id)
        .order_by(AssistantRequest.created_at.desc())
        .limit(limit)
    )
    if feature in common.FEATURE_LABELS:
        query = query.where(AssistantRequest.feature == feature)
    return list(db.scalars(query))


def get(db: Session, actor: Actor, request_id: uuid.UUID) -> AssistantRequest:
    """One of the actor's own requests; anyone else's is not found."""
    row = db.get(AssistantRequest, request_id)
    if row is None or row.user_id != actor.user_id:
        raise NotFound()
    return row


def purge(db: Session) -> int:
    days = common.settings(db).history_days
    cutoff = clock.now() - timedelta(days=days)
    result = db.execute(delete(AssistantRequest).where(AssistantRequest.created_at < cutoff))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
