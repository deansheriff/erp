"""Department channels (ADR 0034): one per active department, with members from the org
chart (everyone with an account holding a position in the department or below it).
Synced by a task, and for the viewer when they open chat. Nobody joins or leaves by
hand; a deactivated department's channel turns read-only."""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import clock
from app.identity.models import User
from app.modules.chat import common
from app.modules.chat.models import ChatMember, Conversation
from app.modules.hr import service as people
from app.org import service as org
from app.org.models import Department
from app.realtime import broker as realtime


@dataclass(frozen=True, slots=True)
class Synced:
    added: int
    removed: int


def _wanted(db: Session) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Active department → the accounts that should be in its channel."""
    holders = org.department_people(db, clock.today())
    employees: dict[uuid.UUID, set[uuid.UUID]] = {}
    for department_id in holders:
        employees[department_id] = {
            e
            for sub in org.departments_under(db, department_id)
            if sub in holders
            for e in holders[sub].holders
        }
    accounts = people.accounts_of(db, {e for es in employees.values() for e in es})
    active = set(db.scalars(select(User.id).where(User.status == "active")))
    return {
        d: {accounts[e] for e in es if e in accounts and accounts[e] in active}
        for d, es in employees.items()
    }


def _channels(db: Session) -> dict[uuid.UUID, Conversation]:
    """A channel for every department, active or not; new active ones are created."""
    found = {
        c.department_id: c
        for c in db.scalars(select(Conversation).where(Conversation.kind == "channel"))
        if c.department_id
    }
    for dept in db.scalars(select(Department)):
        channel = found.get(dept.id)
        if channel is None and dept.is_active:
            channel = Conversation(kind="channel", title=dept.name, department_id=dept.id)
            db.add(channel)
            db.flush()
            found[dept.id] = channel
        if channel is not None:
            channel.title = dept.name
            channel.read_only = not dept.is_active
    return found


def _latest(db: Session, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return common.latest_message_id(db, conversation_id)


def sync(db: Session, *, only_user: uuid.UUID | None = None) -> Synced:
    """Bring channel membership in line with the org chart, for everyone or one user."""
    wanted = _wanted(db)
    added = removed = 0
    for department_id, channel in _channels(db).items():
        should = wanted.get(department_id, set()) if not channel.read_only else set()
        query = select(ChatMember).where(ChatMember.conversation_id == channel.id)
        if only_user is not None:
            query = query.where(ChatMember.user_id == only_user)
            should = should & {only_user}
        current = {m.user_id: m for m in db.scalars(query)}
        if channel.read_only:
            should = set(current)  # a read-only channel keeps its last members
        latest = _latest(db, channel.id)
        for user_id in should - set(current):
            db.add(
                ChatMember(conversation_id=channel.id, user_id=user_id, last_read_message_id=latest)
            )
            added += 1
        gone = set(current) - should
        if gone:
            db.execute(
                delete(ChatMember).where(
                    ChatMember.conversation_id == channel.id, ChatMember.user_id.in_(gone)
                )
            )
            removed += len(gone)
        if (should - set(current)) or gone:
            realtime.notify_after_commit(
                db,
                (should - set(current)) | gone,
                {"type": "members", "conversation": str(channel.id)},
            )
    db.flush()
    return Synced(added, removed)


def moderators(db: Session, conversation: Conversation) -> list[str]:
    ids = common.channel_moderators(db, conversation)
    return sorted(common.user_names(db, ids).values())
