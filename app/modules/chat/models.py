"""Chat (ADR 0034): conversations (direct, group, department channel), their members,
messages with mentions, reactions, pins and attachments, settings, and the unread
emails already sent."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

KINDS = ("direct", "group", "channel")
ROLES = ("member", "admin")
REACTIONS = ("👍", "❤️", "😂", "🎉", "😮", "🙏")
BODY_LIMIT = 4000


class Conversation(IdMixin, Base):
    """A direct message (two members, one per pair), a group or a department channel."""

    __tablename__ = "chat_conversations"
    __table_args__ = (
        CheckConstraint("kind IN ('direct', 'group', 'channel')", name="kind"),
        CheckConstraint("kind <> 'channel' OR department_id IS NOT NULL", name="channel_dept"),
        CheckConstraint("kind <> 'direct' OR direct_key IS NOT NULL", name="direct_key"),
    )

    kind: Mapped[str] = mapped_column(String(10))
    title: Mapped[str | None] = mapped_column(String(120))
    # The two user ids, sorted and joined, so a pair has one direct conversation.
    direct_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id"), unique=True
    )
    read_only: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_message_at: Mapped[datetime | None]


class ChatMember(IdMixin, Base):
    """A current member. Removing someone deletes the row, and with it their access."""

    __tablename__ = "chat_members"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id"),
        CheckConstraint("role IN ('member', 'admin')", name="role"),
        Index("ix_chat_members_user_id", "user_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(10), server_default="member")
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Message ids are UUIDv7, so "after the last read one" is a plain comparison.
    last_read_message_id: Mapped[uuid.UUID | None]


class Message(IdMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_conversation_id_id", "conversation_id", "id"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    edited_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class Mention(IdMixin, Base):
    __tablename__ = "chat_mentions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id"),
        Index("ix_chat_mentions_user_id", "user_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class Reaction(IdMixin, Base):
    __tablename__ = "chat_reactions"
    __table_args__ = (UniqueConstraint("message_id", "user_id", "emoji"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    emoji: Mapped[str] = mapped_column(String(16))


class Pin(IdMixin, Base):
    __tablename__ = "chat_pins"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    pinned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    pinned_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Attachment(IdMixin, Base):
    __tablename__ = "chat_attachments"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"))
    name: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)


class ChatSettings(Base):
    """One row (id 1): the edit window, the unread email delay and retention (confirm)."""

    __tablename__ = "chat_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="one_row"),
        CheckConstraint("edit_minutes BETWEEN 0 AND 1440", name="edit"),
        CheckConstraint("email_after_minutes BETWEEN 5 AND 1440", name="email"),
        CheckConstraint("retention_days IS NULL OR retention_days >= 30", name="retention"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    edit_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    email_after_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    retention_days: Mapped[int | None] = mapped_column(Integer)


class EmailNotice(IdMixin, Base):
    """An unread email already sent: one per person, conversation and day."""

    __tablename__ = "chat_email_notices"
    __table_args__ = (UniqueConstraint("user_id", "conversation_id", "day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    day: Mapped[date] = mapped_column(Date)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())
