"""Unread emails, retention and settings (ADR 0034).

- A mention, or a direct message, still unread after the set delay gets one email per
  person, per conversation, per day. The email never includes the message.
- When a retention period is set, older messages and their attachments are deleted.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.config import get_settings
from app.core.errors import ValidationFailed
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.mail import service as mail
from app.modules.chat import common
from app.modules.chat.models import (
    Attachment,
    ChatMember,
    Conversation,
    EmailNotice,
    Mention,
    Message,
)


def _due(db: Session, cutoff_after: uuid.UUID | None) -> list[tuple[Message, uuid.UUID]]:
    """(message, recipient) pairs: unread mentions and direct messages sent before the
    delay ran out, newest first."""
    delay = timedelta(minutes=common.settings(db).email_after_minutes)
    before = clock.now() - delay
    since = clock.now() - timedelta(days=1)
    unread = or_(
        ChatMember.last_read_message_id.is_(None), Message.id > ChatMember.last_read_message_id
    )
    mentions = (
        select(Message, Mention.user_id)
        .join(Mention, Mention.message_id == Message.id)
        .join(
            ChatMember,
            and_(
                ChatMember.conversation_id == Message.conversation_id,
                ChatMember.user_id == Mention.user_id,
            ),
        )
        .where(Message.created_at <= before, Message.created_at > since, unread)
    )
    direct = (
        select(Message, ChatMember.user_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(ChatMember, ChatMember.conversation_id == Message.conversation_id)
        .where(
            Conversation.kind == "direct",
            ChatMember.user_id != Message.sender_id,
            Message.created_at <= before,
            Message.created_at > since,
            unread,
        )
    )
    rows: list[tuple[Message, uuid.UUID]] = []
    for query in (mentions, direct):
        for message, user_id in db.execute(query.where(Message.deleted_at.is_(None))).all():
            if cutoff_after is None or message.id > cutoff_after:
                rows.append((message, user_id))
    return sorted(rows, key=lambda r: r[0].id, reverse=True)


def send_unread_emails(db: Session, organisation: str) -> int:
    today = clock.today()
    sent = 0
    done: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for message, user_id in _due(db, None):
        key = (user_id, message.conversation_id)
        if key in done:
            continue
        done.add(key)
        already = db.scalar(
            select(EmailNotice.id).where(
                EmailNotice.user_id == user_id,
                EmailNotice.conversation_id == message.conversation_id,
                EmailNotice.day == today,
            )
        )
        user = db.get(User, user_id)
        conversation = db.get(Conversation, message.conversation_id)
        if already or user is None or user.status != "active" or conversation is None:
            continue
        sender = common.user_names(db, {message.sender_id}).get(message.sender_id, "Someone")  # type: ignore[arg-type]
        db.add(EmailNotice(user_id=user_id, conversation_id=conversation.id, day=today))
        mail.queue(
            db,
            to=user.email,
            template="chat_unread",
            context={
                "organisation": organisation,
                "name": user.full_name,
                "sender": sender,
                "where": (
                    "a direct message"
                    if conversation.kind == "direct"
                    else common.title(db, conversation, user_id)
                ),
                "link": f"{get_settings().base_url}/chat/{conversation.id}",
            },
        )
        sent += 1
    db.flush()
    return sent


def purge_old(db: Session) -> int:
    """Delete messages (and their attachments) older than the retention period."""
    days = common.settings(db).retention_days
    if not days:
        return 0
    cutoff = clock.now() - timedelta(days=days)
    old = list(db.scalars(select(Message.id).where(Message.created_at < cutoff).limit(5000)))
    if not old:
        return 0

    for attachment in db.scalars(select(Attachment).where(Attachment.message_id.in_(old))):
        files.delete_by_system(db, attachment.file_id, "chat retention")
    db.execute(delete(Message).where(Message.id.in_(old)))
    db.flush()
    record_by(
        db,
        None,
        "chat.retention.purge",
        entity_type="chat",
        entity_id="messages",
        changes={"deleted": len(old), "older_than_days": days},
    )
    return len(old)


@dataclass(frozen=True, slots=True)
class SettingsInput:
    edit_minutes: str
    email_after_minutes: str
    retention_days: str


def _number(text: str, key: str, low: int, high: int, errors: dict[str, str]) -> int | None:
    raw = (text or "").strip().replace(",", "")
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        errors[key] = f"Enter a number from {low} to {high:,}."
        return None
    return value


def save_settings(db: Session, actor: Actor, data: SettingsInput) -> None:
    common.require(db, actor, common.SETTINGS)
    errors: dict[str, str] = {}
    edit = _number(data.edit_minutes, "edit_minutes", 0, 1440, errors)
    email = _number(data.email_after_minutes, "email_after_minutes", 5, 1440, errors)
    retention = (
        _number(data.retention_days, "retention_days", 30, 36500, errors)
        if data.retention_days.strip()
        else None
    )
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = common.settings(db)
    assert edit is not None and email is not None  # noqa: S101
    row.edit_minutes, row.email_after_minutes, row.retention_days = edit, email, retention
    db.flush()
    record_by(
        db,
        actor,
        "chat.settings.update",
        entity_type="chat_settings",
        entity_id="1",
        changes={"edit_minutes": edit, "email_after_minutes": email, "retention_days": retention},
    )
