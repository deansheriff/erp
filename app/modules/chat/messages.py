"""Messages (ADR 0034): sending (with an attachment and mentions), editing within the
window, deleting, reactions, pins, marking read, and the views the page renders. Every
call checks the actor is a current member of the conversation."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.chat import common
from app.modules.chat.models import (
    BODY_LIMIT,
    REACTIONS,
    Attachment,
    ChatMember,
    Mention,
    Message,
    Pin,
    Reaction,
)

PAGE = 50


# ------------------------------------------------------------------ mentions


def _candidates(names: dict[uuid.UUID, str]) -> dict[str, uuid.UUID]:
    """Lower-cased mention text → person: full names, and first names that are unique."""
    out: dict[str, uuid.UUID] = {}
    firsts: dict[str, list[uuid.UUID]] = {}
    for user_id, name in names.items():
        full = " ".join(name.split()).lower()
        if full:
            out[full] = user_id
            firsts.setdefault(full.split()[0], []).append(user_id)
    for first, ids in firsts.items():
        if len(ids) == 1:
            out.setdefault(first, ids[0])
    return out


def _pattern(candidates: dict[str, uuid.UUID]) -> re.Pattern[str] | None:
    if not candidates:
        return None
    options = "|".join(re.escape(c) for c in sorted(candidates, key=len, reverse=True))
    return re.compile(rf"(?<![\w@])@({options})(?!\w)", re.IGNORECASE)


def find_mentions(body: str, names: dict[uuid.UUID, str]) -> set[uuid.UUID]:
    candidates = _candidates(names)
    pattern = _pattern(candidates)
    if pattern is None:
        return set()
    return {candidates[m.group(1).lower()] for m in pattern.finditer(body)}


@dataclass(frozen=True, slots=True)
class Segment:
    text: str
    mention: bool = False
    me: bool = False


def segments(body: str, names: dict[uuid.UUID, str], viewer: uuid.UUID) -> list[Segment]:
    """The body split into plain text and mentions, for the template to escape."""
    candidates = _candidates(names)
    pattern = _pattern(candidates)
    if pattern is None:
        return [Segment(body)]
    out: list[Segment] = []
    at = 0
    for m in pattern.finditer(body):
        if m.start() > at:
            out.append(Segment(body[at : m.start()]))
        out.append(Segment(m.group(0), True, candidates[m.group(1).lower()] == viewer))
        at = m.end()
    if at < len(body):
        out.append(Segment(body[at:]))
    return out


def _member_names(db: Session, conversation_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return common.user_names(db, set(common.member_ids(db, conversation_id)))


def _save_mentions(db: Session, message: Message, names: dict[uuid.UUID, str]) -> None:
    found = find_mentions(message.body, names) - {message.sender_id}
    existing = set(db.scalars(select(Mention.user_id).where(Mention.message_id == message.id)))
    db.add_all(Mention(message_id=message.id, user_id=u) for u in found - existing)


# ------------------------------------------------------------------ sending and changing


@dataclass(frozen=True, slots=True)
class Upload:
    filename: str | None
    mime: str | None
    data: bytes


def _clean_body(text: str) -> str:
    body = (text or "").strip()
    if len(body) > BODY_LIMIT:
        raise ValidationFailed(
            field_errors={"body": f"Keep a message under {BODY_LIMIT:,} characters."}
        )
    return body


def send(
    db: Session, actor: Actor, conversation_id: uuid.UUID, body: str, upload: Upload | None = None
) -> uuid.UUID:
    found = common.access(db, actor, conversation_id, lock=True)
    if found.conversation.read_only:
        raise Conflict("This channel is read-only: its department has closed.")
    text = _clean_body(body)
    if not text and not (upload and upload.data):
        raise ValidationFailed(field_errors={"body": "Write a message or attach a file."})
    message = Message(conversation_id=conversation_id, sender_id=actor.user_id, body=text)
    db.add(message)
    db.flush()
    if upload and upload.data:
        file_id = files.upload(
            db,
            actor,
            purpose_key="chat_attachment",
            entity_id=str(message.id),
            filename=upload.filename,
            declared_mime=upload.mime,
            data=upload.data,
        )
        db.add(
            Attachment(
                message_id=message.id,
                file_id=file_id,
                name=files.sanitise_filename(upload.filename),
                size_bytes=len(upload.data),
            )
        )
    _save_mentions(db, message, _member_names(db, conversation_id))
    found.conversation.last_message_at = clock.now()
    found.member.last_read_message_id = message.id  # your own message is read
    db.flush()
    common.notify(db, conversation_id, "message", message=str(message.id))
    return message.id


def _message_in(db: Session, found: common.Access, message_id: uuid.UUID) -> Message:
    message = db.get(Message, message_id)
    if message is None or message.conversation_id != found.conversation.id:
        raise NotFound()
    return message


def edit(
    db: Session, actor: Actor, conversation_id: uuid.UUID, message_id: uuid.UUID, body: str
) -> None:
    found = common.access(db, actor, conversation_id)
    message = _message_in(db, found, message_id)
    if message.sender_id != actor.user_id or message.deleted_at is not None:
        raise PermissionDenied("You can only edit your own messages.")
    window = common.settings(db).edit_minutes
    if clock.now() - message.created_at > timedelta(minutes=window):
        raise Conflict(f"Messages can only be edited for {window} minutes after sending.")
    text = _clean_body(body)
    if not text:
        raise ValidationFailed(
            field_errors={"body": "A message can't be empty. Delete it instead."}
        )
    if text != message.body:
        message.body, message.edited_at = text, clock.now()
        _save_mentions(db, message, _member_names(db, conversation_id))
        db.flush()
        common.notify(db, conversation_id, "changed", message=str(message.id))


def delete_message(
    db: Session, actor: Actor, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    found = common.access(db, actor, conversation_id)
    message = _message_in(db, found, message_id)
    if message.deleted_at is not None:
        return
    own = message.sender_id == actor.user_id
    if not own and not common.is_moderator(db, found, actor.user_id):
        raise PermissionDenied("You can only delete your own messages.")
    for attachment in db.scalars(select(Attachment).where(Attachment.message_id == message.id)):
        # The delete is already authorised (own message, or a moderator's), so the files
        # go with it as a system deletion.
        files.delete_by_system(db, attachment.file_id, "chat message deleted")
    db.execute(delete(Attachment).where(Attachment.message_id == message.id))
    db.execute(delete(Reaction).where(Reaction.message_id == message.id))
    db.execute(delete(Pin).where(Pin.message_id == message.id))
    db.execute(delete(Mention).where(Mention.message_id == message.id))
    message.body, message.deleted_at, message.deleted_by = "", clock.now(), actor.user_id
    db.flush()
    if not own:
        record_by(
            db,
            actor,
            "chat.message.delete",
            entity_type="chat_message",
            entity_id=str(message.id),
            changes={"sender": str(message.sender_id)},
        )
    common.notify(db, conversation_id, "changed", message=str(message.id))


def react(
    db: Session, actor: Actor, conversation_id: uuid.UUID, message_id: uuid.UUID, emoji: str
) -> None:
    """Toggle a reaction."""
    found = common.access(db, actor, conversation_id)
    message = _message_in(db, found, message_id)
    if emoji not in REACTIONS:
        raise ValidationFailed(field_errors={"emoji": "Choose one of the reactions."})
    if message.deleted_at is not None:
        raise Conflict("This message was deleted.")
    mine = db.scalars(
        select(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.user_id == actor.user_id,
            Reaction.emoji == emoji,
        )
    ).one_or_none()
    if mine is None:
        db.add(Reaction(message_id=message.id, user_id=actor.user_id, emoji=emoji))
    else:
        db.delete(mine)
    db.flush()
    common.notify(db, conversation_id, "changed", message=str(message.id))


def pin(db: Session, actor: Actor, conversation_id: uuid.UUID, message_id: uuid.UUID) -> bool:
    """Toggle a pin; True when it's now pinned."""
    found = common.access(db, actor, conversation_id)
    message = _message_in(db, found, message_id)
    if message.deleted_at is not None:
        raise Conflict("This message was deleted.")
    current = db.scalars(select(Pin).where(Pin.message_id == message.id)).one_or_none()
    if current is None:
        db.add(Pin(message_id=message.id, conversation_id=conversation_id, pinned_by=actor.user_id))
    else:
        db.delete(current)
    db.flush()
    common.notify(db, conversation_id, "changed", message=str(message.id))
    return current is None


def mark_read(db: Session, actor: Actor, conversation_id: uuid.UUID) -> None:
    """Move the actor's read mark to the newest message."""
    found = common.access(db, actor, conversation_id)
    latest = common.latest_message_id(db, conversation_id)
    if latest is not None and (
        found.member.last_read_message_id is None or latest > found.member.last_read_message_id
    ):
        found.member.last_read_message_id = latest
        db.flush()
        common.notify(db, conversation_id, "read")


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class ReactionView:
    emoji: str
    count: int
    mine: bool
    names: list[str]


@dataclass(slots=True)
class MessageView:
    message: Message
    sender: str
    segments: list[Segment]
    attachments: list[Attachment] = field(default_factory=list)
    reactions: list[ReactionView] = field(default_factory=list)
    pinned_by: str | None = None
    mine: bool = False
    can_edit: bool = False
    can_delete: bool = False
    mentions_me: bool = False
    starts_group: bool = True  # a new sender or a gap: show the name and time
    new_day: bool = True  # the first message of a day: show the date
    day: date | None = None  # in the organisation's timezone


def _views(
    db: Session,
    found: common.Access,
    actor: Actor,
    rows: list[Message],
    previous: Message | None = None,
) -> list[MessageView]:
    """Views of `rows`, oldest first. `previous` is the message just before them, so
    grouping and day breaks are right when only newer messages are fetched."""
    if not rows:
        return []
    ids = [m.id for m in rows]
    names = _member_names(db, found.conversation.id)
    others = {m.sender_id for m in rows if m.sender_id and m.sender_id not in names}
    senders = {**names, **common.user_names(db, others)}
    attachments: dict[uuid.UUID, list[Attachment]] = {}
    for a in db.scalars(select(Attachment).where(Attachment.message_id.in_(ids))):
        attachments.setdefault(a.message_id, []).append(a)
    reactions: dict[uuid.UUID, dict[str, list[uuid.UUID]]] = {}
    for r in db.scalars(select(Reaction).where(Reaction.message_id.in_(ids)).order_by(Reaction.id)):
        reactions.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)
    pins = {p.message_id: p for p in db.scalars(select(Pin).where(Pin.message_id.in_(ids)))}
    mentioned = set(
        db.scalars(
            select(Mention.message_id).where(
                Mention.message_id.in_(ids), Mention.user_id == actor.user_id
            )
        )
    )
    window = timedelta(minutes=common.settings(db).edit_minutes)
    moderator = common.is_moderator(db, found, actor.user_id)
    now = clock.now()
    out: list[MessageView] = []
    for m in rows:
        mine = m.sender_id == actor.user_id
        alive = m.deleted_at is None
        out.append(
            MessageView(
                m,
                senders.get(m.sender_id, "Former colleague") if m.sender_id else "",
                segments(m.body, names, actor.user_id) if alive else [],
                attachments.get(m.id, []),
                [
                    ReactionView(
                        emoji,
                        len(users),
                        actor.user_id in users,
                        [senders.get(u, "") for u in users],
                    )
                    for emoji in REACTIONS
                    if (users := reactions.get(m.id, {}).get(emoji))
                ],
                _pinned_by(pins.get(m.id), senders),
                mine,
                mine and alive and now - m.created_at <= window,
                alive and (mine or moderator),
                m.id in mentioned,
                previous is None
                or previous.sender_id != m.sender_id
                or m.created_at - previous.created_at > timedelta(minutes=10)
                or clock.local_date(m.created_at) != clock.local_date(previous.created_at),
                previous is None
                or clock.local_date(m.created_at) != clock.local_date(previous.created_at),
            )
        )
        out[-1].day = clock.local_date(m.created_at)
        previous = m
    return out


def _pinned_by(pin: Pin | None, senders: dict[uuid.UUID, str]) -> str | None:
    if pin is None:
        return None
    return senders.get(pin.pinned_by, "") if pin.pinned_by else ""


@dataclass(frozen=True, slots=True)
class Page:
    access: common.Access
    messages: list[MessageView]
    more_before: bool


def page(
    db: Session,
    actor: Actor,
    conversation_id: uuid.UUID,
    *,
    before: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
) -> Page:
    """Up to 50 messages: the latest, those before one (older), or after one (newer)."""
    found = common.access(db, actor, conversation_id)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if after is not None:
        rows = list(db.scalars(query.where(Message.id > after).order_by(Message.id).limit(PAGE)))
        more = False
    else:
        if before is not None:
            query = query.where(Message.id < before)
        newest_first = list(db.scalars(query.order_by(Message.id.desc()).limit(PAGE + 1)))
        more = len(newest_first) > PAGE
        rows = list(reversed(newest_first[:PAGE]))
    previous = _before(db, conversation_id, rows[0].id) if rows else None
    return Page(found, _views(db, found, actor, rows, previous), more)


def _before(db: Session, conversation_id: uuid.UUID, message_id: uuid.UUID) -> Message | None:
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.id < message_id)
        .order_by(Message.id.desc())
        .limit(1)
    ).first()


def one(
    db: Session, actor: Actor, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> MessageView:
    found = common.access(db, actor, conversation_id)
    message = _message_in(db, found, message_id)
    return _views(db, found, actor, [message], _before(db, conversation_id, message.id))[0]


@dataclass(frozen=True, slots=True)
class Receipt:
    names: list[str]  # who has seen the latest message (direct and group)
    count: int  # how many have (channels show only this)
    of: int  # members other than the sender
    show_names: bool
    mine: bool  # the latest message is the viewer's own


def receipt(db: Session, actor: Actor, conversation_id: uuid.UUID) -> Receipt | None:
    """Who has seen the latest message, other than its sender."""
    found = common.access(db, actor, conversation_id)
    latest = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.deleted_at.is_(None))
        .order_by(Message.id.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    members = list(
        db.scalars(select(ChatMember).where(ChatMember.conversation_id == conversation_id))
    )
    others = [m for m in members if m.user_id != latest.sender_id]
    seen = [
        m.user_id
        for m in others
        if m.last_read_message_id is not None and m.last_read_message_id >= latest.id
    ]
    show = found.conversation.kind != "channel"
    names = sorted(common.user_names(db, set(seen)).values()) if show else []
    return Receipt(names, len(seen), len(others), show, latest.sender_id == actor.user_id)


def pinned(db: Session, actor: Actor, conversation_id: uuid.UUID) -> list[tuple[Pin, Message]]:
    common.access(db, actor, conversation_id)
    return list(
        db.execute(
            select(Pin, Message)
            .join(Message, Message.id == Pin.message_id)
            .where(Pin.conversation_id == conversation_id)
            .order_by(Pin.pinned_at.desc())
            .limit(3)
        ).all()
    )


def last_id(db: Session, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return common.latest_message_id(db, conversation_id)
