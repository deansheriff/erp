"""Phase 12: direct messages, groups and department channels; membership checked on every
action; mentions, edits, deletes, reactions, pins, unread counts and receipts;
attachments for members only; notices after commit; unread emails; settings and
retention (ADR 0034)."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.models import User
from app.mail.models import MailOutbox
from app.modules.chat import channels, conversations, messages, notices
from app.modules.chat.messages import Upload
from app.modules.chat.models import ChatMember, ChatSettings, Mention, Message
from app.org.models import Department, PositionAssignment
from app.realtime import broker as realtime
from tests.chat.conftest import Chat
from tests.files.conftest import png

pytestmark = pytest.mark.db


def _group(c: Chat, *who: str, title: str = "Month-end close", by: str = "Bola"):  # type: ignore[no-untyped-def]
    return conversations.create_group(c.db, c.actor(by), title, [str(c.user(w)) for w in who])


def test_direct_messages_and_groups_check_membership(chat: Chat) -> None:
    c, a = chat, chat.actor
    with pytest.raises(ValidationFailed):
        conversations.open_direct(c.db, a("Efe"), str(c.user("Efe")))
    dm = conversations.open_direct(c.db, a("Efe"), str(c.user("Gbenga")))
    assert conversations.open_direct(c.db, a("Gbenga"), str(c.user("Efe"))) == dm
    messages.send(c.db, a("Efe"), dm, "Have you posted the accruals?")
    with pytest.raises(NotFound):  # not a member: it doesn't exist for them
        messages.page(c.db, a("Hauwa"), dm)
    with pytest.raises(NotFound):
        messages.send(c.db, a("Hauwa"), dm, "Hello?")
    with pytest.raises(Conflict):
        conversations.leave(c.db, a("Efe"), dm)

    group = _group(c, "Efe", "Gbenga")
    with pytest.raises(PermissionDenied):
        conversations.add_members(c.db, a("Efe"), group, [str(c.user("Femi"))])
    conversations.add_members(c.db, a("Bola"), group, [str(c.user("Femi"))])
    conversations.make_admin(c.db, a("Bola"), group, str(c.user("Efe")))
    conversations.remove_member(c.db, a("Efe"), group, str(c.user("Gbenga")))
    with pytest.raises(NotFound):  # removed: the whole history goes with it
        messages.page(c.db, a("Gbenga"), group)
    conversations.leave(c.db, a("Bola"), group)
    conversations.leave(c.db, a("Efe"), group)
    roles = dict(
        c.db.execute(
            select(ChatMember.user_id, ChatMember.role).where(ChatMember.conversation_id == group)
        ).all()
    )
    assert roles == {c.user("Femi"): "admin"}  # the last one standing takes over
    titles = {r.title for r in conversations.list_for(c.db, a("Efe"))}
    assert "Gbenga Test" in titles and "Month-end close" not in titles


def test_messages_mentions_edits_deletes_reactions_and_pins(chat: Chat) -> None:
    c, a = chat, chat.actor
    group = _group(c, "Efe", "Gbenga", "Femi")
    first = messages.send(c.db, a("Bola"), group, "@Efe and @Gbenga Test: accruals by Friday")
    mentioned = set(c.db.scalars(select(Mention.user_id).where(Mention.message_id == first)))
    assert mentioned == {c.user("Efe"), c.user("Gbenga")}
    view = messages.page(c.db, a("Efe"), group).messages[0]
    assert view.mentions_me and [s.text for s in view.segments if s.mention] == [
        "@Efe",
        "@Gbenga Test",
    ]

    assert conversations.unread_counts(c.db, c.user("Efe")) == {group: 1}
    messages.mark_read(c.db, a("Efe"), group)
    assert conversations.unread_counts(c.db, c.user("Efe")) == {}
    receipt = messages.receipt(c.db, a("Bola"), group)
    assert receipt is not None and receipt.names == ["Efe Test"] and receipt.of == 3

    reply = messages.send(c.db, a("Efe"), group, "On it")
    messages.edit(c.db, a("Efe"), group, reply, "On it, by Thursday")
    with pytest.raises(PermissionDenied):
        messages.edit(c.db, a("Gbenga"), group, reply, "Not mine")
    row = c.db.get(Message, reply)
    assert row is not None
    row.created_at = clock.now() - timedelta(minutes=16)
    with pytest.raises(Conflict, match="15 minutes"):
        messages.edit(c.db, a("Efe"), group, reply, "Too late")

    messages.react(c.db, a("Gbenga"), group, first, "👍")
    messages.react(c.db, a("Femi"), group, first, "👍")
    messages.react(c.db, a("Femi"), group, first, "👍")  # toggled off again
    with pytest.raises(ValidationFailed):
        messages.react(c.db, a("Femi"), group, first, "🍕")
    assert messages.pin(c.db, a("Gbenga"), group, first) is True
    view = messages.one(c.db, a("Bola"), group, first)
    assert [(r.emoji, r.count) for r in view.reactions] == [("👍", 1)]
    assert view.pinned_by == "Gbenga Test"

    with pytest.raises(PermissionDenied):  # not yours, and not a group admin
        messages.delete_message(c.db, a("Gbenga"), group, reply)
    messages.delete_message(c.db, a("Bola"), group, first)  # the admin may
    gone = messages.one(c.db, a("Efe"), group, first)
    assert gone.message.body == "" and gone.reactions == [] and gone.pinned_by is None
    with pytest.raises(Conflict):
        messages.react(c.db, a("Efe"), group, first, "👍")


def test_department_channels_follow_the_org_chart(chat: Chat) -> None:
    c, a = chat, chat.actor
    channels.sync(c.db)
    finance = c.channel("FIN")
    members = set(
        c.db.scalars(select(ChatMember.user_id).where(ChatMember.conversation_id == finance))
    )
    assert members == {c.user(w) for w in ("Bola", "Efe", "Gbenga", "Femi")}
    assert [r.title for r in conversations.list_for(c.db, a("Hauwa"))] == []

    note = messages.send(c.db, a("Efe"), finance, "Payment run is ready")
    with pytest.raises(PermissionDenied):  # Gbenga isn't the head
        messages.delete_message(c.db, a("Gbenga"), finance, note)
    messages.delete_message(c.db, a("Bola"), finance, note)  # the Head of Finance moderates
    with pytest.raises(Conflict):
        conversations.leave(c.db, a("Efe"), finance)

    c.db.execute(
        update(PositionAssignment)
        .where(PositionAssignment.employee_id == c.hr.employee("Efe"))
        .values(end_date=clock.today() - timedelta(days=1))
    )
    c.hr.org.changed()
    channels.sync(c.db)
    with pytest.raises(NotFound):
        messages.page(c.db, a("Efe"), finance)

    c.db.execute(
        update(Department)
        .where(Department.id == c.hr.org.departments["TRE"])
        .values(is_active=False)
    )
    c.hr.org.changed()
    channels.sync(c.db)
    with pytest.raises(Conflict, match="read-only"):
        messages.send(c.db, a("Femi"), c.channel("TRE"), "Anyone?")


def test_attachments_are_for_members_only(chat: Chat) -> None:
    c, a = chat, chat.actor
    dm = conversations.open_direct(c.db, a("Efe"), str(c.user("Gbenga")))
    sent = messages.send(c.db, a("Efe"), dm, "", Upload("statement.png", "image/png", png()))
    page = messages.page(c.db, a("Gbenga"), dm)
    file_id = page.messages[0].attachments[0].file_id
    with pytest.raises(Conflict, match="viruses"):  # allowed; still being scanned
        files.open_for_download(c.db, a("Gbenga"), file_id)
    with pytest.raises(NotFound):
        files.open_for_download(c.db, a("Hauwa"), file_id)
    with pytest.raises(NotFound):  # Ada administers the organisation, but isn't in it
        files.open_for_download(c.db, a("Ada"), file_id)
    messages.delete_message(c.db, a("Efe"), dm, sent)
    with pytest.raises(NotFound):
        files.open_for_download(c.db, a("Gbenga"), file_id)


def test_notices_go_to_members_after_commit(chat: Chat) -> None:
    c, a = chat, chat.actor
    dm = conversations.open_direct(c.db, a("Efe"), str(c.user("Gbenga")))
    messages.send(c.db, a("Efe"), dm, "Ping")
    assert c.broker.sent == []  # nothing until the transaction commits
    c.db.commit()
    sent = {(ch, json.loads(m)["type"]) for ch, m in c.broker.sent}
    assert sent == {
        (realtime.channel(c.user("Efe")), "message"),
        (realtime.channel(c.user("Gbenga")), "message"),
    }
    assert all("Ping" not in m for _, m in c.broker.sent)  # no content in a notice


def test_unread_mentions_and_direct_messages_are_emailed_once(chat: Chat, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c, a = chat, chat.actor
    dm = conversations.open_direct(c.db, a("Efe"), str(c.user("Gbenga")))
    messages.send(c.db, a("Efe"), dm, "Quick question")
    messages.send(c.db, a("Efe"), dm, "And another")
    group = _group(c, "Efe", "Femi", "Dayo")
    messages.send(c.db, a("Bola"), group, "@Femi and @Dayo please check")
    messages.send(c.db, a("Bola"), group, "No mention for Efe here")
    messages.mark_read(c.db, a("Dayo"), group)  # Dayo has seen it: no email
    assert notices.send_unread_emails(c.db, "Adeyemi Holdings") == 0  # too soon
    later = clock.now() + timedelta(minutes=31)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert notices.send_unread_emails(c.db, "Adeyemi Holdings") == 2  # Gbenga and Femi
    messages.send(c.db, a("Efe"), dm, "Still there?")
    monkeypatch.setattr(clock, "now", lambda: later + timedelta(minutes=31))
    assert notices.send_unread_emails(c.db, "Adeyemi Holdings") == 0  # once a day
    mails = list(c.db.scalars(select(MailOutbox).where(MailOutbox.template == "chat_unread")))
    expected = {c.db.get(User, c.user(w)).email for w in ("Gbenga", "Femi")}  # type: ignore[union-attr]
    assert {m.to_address for m in mails} == expected
    assert all("Quick question" not in m.body_text for m in mails)


def test_settings_and_retention(chat: Chat, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c, a = chat, chat.actor
    data = notices.SettingsInput("15", "30", "10")
    with pytest.raises(PermissionDenied):
        notices.save_settings(c.db, a("Efe"), data)
    with pytest.raises(ValidationFailed) as bad:
        notices.save_settings(c.db, a("Ada"), data)
    assert "retention_days" in (bad.value.field_errors or {})
    dm = conversations.open_direct(c.db, a("Efe"), str(c.user("Gbenga")))
    old = messages.send(c.db, a("Efe"), dm, "Ancient history")
    assert notices.purge_old(c.db) == 0  # kept indefinitely by default
    notices.save_settings(c.db, a("Ada"), notices.SettingsInput("5", "60", "30"))
    row = c.db.get(ChatSettings, 1)
    assert row is not None and (row.edit_minutes, row.retention_days) == (5, 30)
    later = clock.now() + timedelta(days=31)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert notices.purge_old(c.db) == 1
    assert c.db.get(Message, old) is None
