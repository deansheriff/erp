"""Conversations (ADR 0034): opening a direct message, groups and their members, and the
list with unread counts. Every call checks the actor is a current member."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.chat import channels, common
from app.modules.chat.models import Attachment, ChatMember, Conversation, Message, Pin
from app.realtime import broker as realtime

TITLE_LIMIT = 120


def people(db: Session, actor: Actor) -> list[tuple[uuid.UUID, str, str]]:
    """Active people who can use chat, other than the actor: (id, name, email)."""
    rows = db.execute(
        select(User.id, User.full_name, User.email)
        .where(User.status == "active", User.id != actor.user_id)
        .order_by(User.full_name)
    ).all()
    return [(u, n, e) for u, n, e in rows if common.can(db, u, common.USE)]


def _usable(db: Session, user_id: uuid.UUID) -> bool:
    user = db.get(User, user_id)
    return user is not None and user.status == "active" and common.can(db, user_id, common.USE)


def _latest(db: Session, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return common.latest_message_id(db, conversation_id)


def open_direct(db: Session, actor: Actor, other: str) -> uuid.UUID:
    """The direct conversation with someone, created the first time."""
    common.require(db, actor, common.USE)
    other_id = common.uuid_or_none(other)
    if other_id is None or other_id == actor.user_id or not _usable(db, other_id):
        raise ValidationFailed(field_errors={"person_id": "Choose someone to message."})
    key = ":".join(sorted((str(actor.user_id), str(other_id))))
    found = db.scalar(select(Conversation.id).where(Conversation.direct_key == key))
    if found is not None:
        return found
    row = Conversation(kind="direct", direct_key=key, created_by=actor.user_id)
    db.add(row)
    db.flush()
    db.add_all(
        [
            ChatMember(conversation_id=row.id, user_id=actor.user_id),
            ChatMember(conversation_id=row.id, user_id=other_id),
        ]
    )
    db.flush()
    return row.id


def _clean_title(text: str) -> str:
    title = " ".join((text or "").split())
    if not title:
        raise ValidationFailed(field_errors={"title": "Give the group a name."})
    if len(title) > TITLE_LIMIT:
        raise ValidationFailed(field_errors={"title": f"Keep it under {TITLE_LIMIT} characters."})
    return title


def create_group(db: Session, actor: Actor, title: str, member_ids: list[str]) -> uuid.UUID:
    common.require(db, actor, common.USE)
    name = _clean_title(title)
    chosen = {u for u in (common.uuid_or_none(m) for m in member_ids) if u}
    chosen.discard(actor.user_id)
    if not chosen:
        raise ValidationFailed(field_errors={"members": "Add at least one person."})
    if not all(_usable(db, u) for u in chosen):
        raise ValidationFailed(field_errors={"members": "Someone chosen can't use chat."})
    row = Conversation(kind="group", title=name, created_by=actor.user_id)
    db.add(row)
    db.flush()
    db.add(ChatMember(conversation_id=row.id, user_id=actor.user_id, role="admin"))
    db.add_all(ChatMember(conversation_id=row.id, user_id=u) for u in chosen)
    db.flush()
    record_by(
        db, actor, "chat.group.create", entity_type="chat_conversation", entity_id=str(row.id)
    )
    realtime.notify_after_commit(db, chosen, {"type": "members", "conversation": str(row.id)})
    return row.id


def _group_admin(db: Session, actor: Actor, conversation_id: uuid.UUID) -> common.Access:
    found = common.access(db, actor, conversation_id, lock=True)
    if found.conversation.kind != "group":
        raise Conflict("Only groups have members you can change.")
    if found.member.role != "admin":
        raise PermissionDenied("Only the group's admins can do that.")
    return found


def set_title(db: Session, actor: Actor, conversation_id: uuid.UUID, title: str) -> None:
    found = _group_admin(db, actor, conversation_id)
    found.conversation.title = _clean_title(title)
    db.flush()
    common.notify(db, conversation_id, "members")


def add_members(db: Session, actor: Actor, conversation_id: uuid.UUID, ids: list[str]) -> int:
    _group_admin(db, actor, conversation_id)
    current = set(common.member_ids(db, conversation_id))
    chosen = {u for u in (common.uuid_or_none(i) for i in ids) if u} - current
    if not chosen:
        raise ValidationFailed(field_errors={"members": "Choose someone who isn't already in."})
    if not all(_usable(db, u) for u in chosen):
        raise ValidationFailed(field_errors={"members": "Someone chosen can't use chat."})
    latest = _latest(db, conversation_id)
    db.add_all(
        ChatMember(conversation_id=conversation_id, user_id=u, last_read_message_id=latest)
        for u in chosen
    )
    db.flush()
    record_by(
        db,
        actor,
        "chat.group.add_members",
        entity_type="chat_conversation",
        entity_id=str(conversation_id),
        changes={"added": sorted(str(u) for u in chosen)},
    )
    common.notify(db, conversation_id, "members")
    return len(chosen)


def _promote_if_needed(db: Session, conversation_id: uuid.UUID) -> None:
    """A group keeps at least one admin: the longest-standing member takes over."""
    members = list(
        db.scalars(
            select(ChatMember)
            .where(ChatMember.conversation_id == conversation_id)
            .order_by(ChatMember.joined_at, ChatMember.id)
        )
    )
    if members and not any(m.role == "admin" for m in members):
        members[0].role = "admin"


def _remove(db: Session, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    others = common.member_ids(db, conversation_id)
    db.execute(
        delete(ChatMember).where(
            ChatMember.conversation_id == conversation_id, ChatMember.user_id == user_id
        )
    )
    db.flush()
    _promote_if_needed(db, conversation_id)
    db.flush()
    realtime.notify_after_commit(
        db, others, {"type": "members", "conversation": str(conversation_id)}
    )


def remove_member(db: Session, actor: Actor, conversation_id: uuid.UUID, user: str) -> None:
    _group_admin(db, actor, conversation_id)
    user_id = common.uuid_or_none(user)
    if user_id is None or user_id not in common.member_ids(db, conversation_id):
        raise NotFound()
    if user_id == actor.user_id:
        raise Conflict("Use Leave group to leave it yourself.")
    _remove(db, conversation_id, user_id)
    record_by(
        db,
        actor,
        "chat.group.remove_member",
        entity_type="chat_conversation",
        entity_id=str(conversation_id),
        changes={"removed": str(user_id)},
    )


def make_admin(db: Session, actor: Actor, conversation_id: uuid.UUID, user: str) -> None:
    _group_admin(db, actor, conversation_id)
    user_id = common.uuid_or_none(user)
    member = db.scalars(
        select(ChatMember).where(
            ChatMember.conversation_id == conversation_id, ChatMember.user_id == user_id
        )
    ).one_or_none()
    if member is None:
        raise NotFound()
    member.role = "admin"
    db.flush()
    common.notify(db, conversation_id, "members")


def leave(db: Session, actor: Actor, conversation_id: uuid.UUID) -> None:
    found = common.access(db, actor, conversation_id, lock=True)
    if found.conversation.kind != "group":
        raise Conflict(
            "You can't leave a direct message."
            if found.conversation.kind == "direct"
            else "Department channel membership follows the org chart."
        )
    _remove(db, conversation_id, actor.user_id)
    record_by(
        db,
        actor,
        "chat.group.leave",
        entity_type="chat_conversation",
        entity_id=str(conversation_id),
    )


# ------------------------------------------------------------------ the list


@dataclass(frozen=True, slots=True)
class ConversationRow:
    conversation: Conversation
    title: str
    unread: int
    preview: str | None
    last_at: datetime | None


def unread_counts(db: Session, user_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Unread messages by conversation: after the last read one, from someone else."""
    rows = db.execute(
        select(Message.conversation_id, func.count())
        .join(
            ChatMember,
            and_(
                ChatMember.conversation_id == Message.conversation_id, ChatMember.user_id == user_id
            ),
        )
        .where(
            or_(
                ChatMember.last_read_message_id.is_(None),
                Message.id > ChatMember.last_read_message_id,
            ),
            or_(Message.sender_id.is_(None), Message.sender_id != user_id),
            Message.deleted_at.is_(None),
        )
        .group_by(Message.conversation_id)
    ).all()
    return {c: int(n) for c, n in rows}


def total_unread(db: Session, user_id: uuid.UUID) -> int:
    return sum(unread_counts(db, user_id).values())


def _previews(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Message]:
    if not ids:
        return {}
    newest = (
        select(Message)
        .where(Message.conversation_id.in_(ids))
        .ext(distinct_on(Message.conversation_id))
        .order_by(Message.conversation_id, Message.id.desc())
    )
    return {m.conversation_id: m for m in db.scalars(newest)}


def _preview_text(db: Session, message: Message | None, viewer: uuid.UUID) -> str | None:
    if message is None:
        return None
    if message.deleted_at is not None:
        return "Message deleted"
    text = " ".join(message.body.split()) or (
        "Sent an attachment"
        if db.scalar(select(Attachment.id).where(Attachment.message_id == message.id))
        else ""
    )
    who = (
        "You"
        if message.sender_id == viewer
        else common.user_names(db, {message.sender_id}).get(message.sender_id, "")
        if message.sender_id
        else ""
    )
    snippet = text[:80] + ("…" if len(text) > 80 else "")
    return f"{who}: {snippet}" if who else snippet


def list_for(db: Session, actor: Actor, *, sync: bool = True) -> list[ConversationRow]:
    """The actor's conversations, most recent first, with unread counts."""
    common.require(db, actor, common.USE)
    if sync:
        channels.sync(db, only_user=actor.user_id)
    conversations = list(
        db.scalars(
            select(Conversation)
            .join(ChatMember, ChatMember.conversation_id == Conversation.id)
            .where(ChatMember.user_id == actor.user_id)
        )
    )
    unread = unread_counts(db, actor.user_id)
    previews = _previews(db, [c.id for c in conversations])
    rows = [
        ConversationRow(
            c,
            common.title(db, c, actor.user_id),
            unread.get(c.id, 0),
            _preview_text(db, previews.get(c.id), actor.user_id),
            c.last_message_at,
        )
        for c in conversations
    ]
    epoch = datetime(2000, 1, 1, tzinfo=clock.now().tzinfo)
    return sorted(rows, key=lambda r: r.last_at or r.conversation.created_at or epoch, reverse=True)


# ------------------------------------------------------------------ details


@dataclass(frozen=True, slots=True)
class MemberRow:
    user_id: uuid.UUID
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class Details:
    access: common.Access
    title: str
    members: list[MemberRow]
    moderators: list[str]
    pins: list[tuple[Pin, Message, str]]  # pin, message, sender name
    files: list[tuple[Attachment, Message, str]]
    is_admin: bool


def details(db: Session, actor: Actor, conversation_id: uuid.UUID) -> Details:
    found = common.access(db, actor, conversation_id)
    members = list(
        db.scalars(select(ChatMember).where(ChatMember.conversation_id == conversation_id))
    )
    pins = list(
        db.execute(
            select(Pin, Message)
            .join(Message, Message.id == Pin.message_id)
            .where(Pin.conversation_id == conversation_id)
            .order_by(Pin.pinned_at.desc())
        ).all()
    )
    files = list(
        db.execute(
            select(Attachment, Message)
            .join(Message, Message.id == Attachment.message_id)
            .where(Message.conversation_id == conversation_id, Message.deleted_at.is_(None))
            .order_by(Message.id.desc())
            .limit(50)
        ).all()
    )
    names = common.user_names(
        db,
        {m.user_id for m in members}
        | {m.sender_id for _, m in pins if m.sender_id}
        | {m.sender_id for _, m in files if m.sender_id},
    )
    return Details(
        found,
        common.title(db, found.conversation, actor.user_id),
        sorted(
            (MemberRow(m.user_id, names.get(m.user_id, ""), m.role) for m in members),
            key=lambda r: (r.role != "admin", r.name),
        ),
        channels.moderators(db, found.conversation),
        [(p, m, names.get(m.sender_id, "") if m.sender_id else "") for p, m in pins],
        [(a, m, names.get(m.sender_id, "") if m.sender_id else "") for a, m in files],
        found.conversation.kind == "group" and found.member.role == "admin",
    )
