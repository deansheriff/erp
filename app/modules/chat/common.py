"""Shared chat helpers (ADR 0034): permissions, settings, membership checks, names and
titles. Membership is the access rule: a non-member gets "not found"."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.core import clock
from app.core.errors import NotFound, PermissionDenied
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.chat.models import ChatMember, ChatSettings, Conversation, Message
from app.modules.hr import service as people
from app.org import service as org
from app.realtime import broker as realtime

USE = "chat.conversation.use"
MODERATE = "chat.channel.moderate"
SETTINGS = "chat.settings.manage"
KIND_LABELS = {"direct": "Direct message", "group": "Group", "channel": "Department channel"}


def can(db: Session, user_id: uuid.UUID, permission: str) -> bool:
    return bool(engine.check(db, user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor.user_id, permission):
        raise PermissionDenied()


def settings(db: Session) -> ChatSettings:
    row = db.get(ChatSettings, 1)
    if row is None:  # seeded by the migration; recreated if it ever goes missing
        row = ChatSettings(id=1, edit_minutes=15, email_after_minutes=30, retention_days=None)
        db.add(row)
        db.flush()
    return row


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Access:
    conversation: Conversation
    member: ChatMember


def access(db: Session, actor: Actor, conversation_id: uuid.UUID, *, lock: bool = False) -> Access:
    """The conversation and the actor's membership, or not found."""
    require(db, actor, USE)
    query = select(ChatMember).where(
        ChatMember.conversation_id == conversation_id, ChatMember.user_id == actor.user_id
    )
    member = db.scalars(query.with_for_update() if lock else query).one_or_none()
    conversation = db.get(Conversation, conversation_id) if member else None
    if member is None or conversation is None:
        raise NotFound()
    return Access(conversation, member)


def member_ids(db: Session, conversation_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.scalars(select(ChatMember.user_id).where(ChatMember.conversation_id == conversation_id))
    )


def user_names(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def title(db: Session, conversation: Conversation, viewer: uuid.UUID) -> str:
    """A direct message is titled by the other person; others by their name."""
    if conversation.kind != "direct":
        return conversation.title or "Untitled"
    others = [u for u in member_ids(db, conversation.id) if u != viewer]
    return user_names(db, set(others)).get(others[0], "Former colleague") if others else "You"


def channel_moderators(db: Session, conversation: Conversation) -> set[uuid.UUID]:
    """The current holders of the department's top positions who have an account."""
    if conversation.kind != "channel" or conversation.department_id is None:
        return set()

    found = org.department_people(db, clock.today()).get(conversation.department_id)
    if found is None:
        return set()
    return set(people.accounts_of(db, set(found.heads)).values())


def is_moderator(db: Session, access: Access, user_id: uuid.UUID) -> bool:
    """Group admins, and for channels their heads and holders of chat.channel.moderate."""
    if access.conversation.kind == "group":
        return access.member.role == "admin"
    if access.conversation.kind == "channel":
        return can(db, user_id, MODERATE) or user_id in channel_moderators(db, access.conversation)
    return False


def notify(db: Session, conversation_id: uuid.UUID, kind: str, **extra: object) -> None:
    """Tell every member's open pages that something changed (after commit)."""
    realtime.notify_after_commit(
        db,
        member_ids(db, conversation_id),
        {"type": kind, "conversation": str(conversation_id), **extra},
    )


def latest_message_id(db: Session, conversation_id: uuid.UUID) -> uuid.UUID | None:
    """The newest message's id (UUIDv7 ids sort by time; Postgres has no max(uuid))."""
    return db.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(1)
    )
