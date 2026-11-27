"""File purpose for chat attachments (ADR 0034): attached to the message by its sender,
and reachable only by current members of its conversation."""

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.chat.models import ChatMember, Message

ALLOWED = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)


def _message(db: Session, entity_id: str) -> Message | None:
    try:
        key = uuid.UUID(entity_id)
    except ValueError:
        return None
    return db.get(Message, key)


def _resolve(db: Session, entity_id: str) -> Resource | None:
    """Owned by the sender, so attaching needs `chat.conversation.use` for oneself."""
    message = _message(db, entity_id)
    return Resource(owner_user_id=message.sender_id) if message is not None else None


def _member(db: Session, user_id: uuid.UUID, entity_id: str) -> bool:
    message = _message(db, entity_id)
    if message is None or message.deleted_at is not None:
        return False
    return (
        db.scalar(
            select(ChatMember.id).where(
                ChatMember.conversation_id == message.conversation_id, ChatMember.user_id == user_id
            )
        )
        is not None
    )


CHAT_ATTACHMENT = FilePurpose(
    key="chat_attachment",
    label="Chat attachment",
    entity_type="chat_message",
    allowed_types=ALLOWED,
    max_bytes=25 * MB,
    attach_permission="chat.conversation.use",
    # Viewing is decided by membership alone (`allow`), not by a permission scope.
    view_permission=None,
    # Deleted attachments are removed after 30 days (confirm).
    retention=timedelta(days=30),
    inline=True,
    classification="directory",
    resolve=_resolve,
    allow=_member,
)

PURPOSES = (CHAT_ATTACHMENT,)
