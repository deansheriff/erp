"""Mail settings, the outbox and delivery (ADR 0012). SMTP itself is replaced by a fake."""

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.core.db import session_scope
from app.mail import service as mail
from app.mail.models import MailOutbox, MailSettings
from app.mail.transport import Message, SendFailed, SmtpConfig
from tests.conftest import Installed

pytestmark = pytest.mark.db


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Message]]:
    outbox: list[Message] = []

    def fake_send(config: SmtpConfig, message: Message) -> None:
        outbox.append(message)

    monkeypatch.setattr("app.mail.service.send", fake_send)
    yield outbox


def test_queued_mail_is_delivered_by_the_worker(installed: Installed, sent: list[Message]) -> None:
    with session_scope() as db:
        mail.queue(
            db,
            to="a@adeyemiholdings.ng",
            template="test",
            context={"name": "Ada", "organisation": "Adeyemi"},
        )
    assert mail.send_pending() == 1
    assert sent[0].to == "a@adeyemiholdings.ng"
    with session_scope() as db:
        row = db.scalars(select(MailOutbox)).one()
        assert row.status == "sent"


def test_failures_retry_then_give_up_with_a_category(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(config: SmtpConfig, message: Message) -> None:
        raise SendFailed("auth_failed")

    monkeypatch.setattr("app.mail.service.send", failing)
    with session_scope() as db:
        mail.queue(
            db,
            to="a@adeyemiholdings.ng",
            template="test",
            context={"name": "Ada", "organisation": "X"},
        )
    for _ in range(mail.MAX_ATTEMPTS):
        with session_scope() as db:
            db.execute(MailOutbox.__table__.update().values(next_attempt_at=MailOutbox.created_at))
        mail.send_pending()
    with session_scope() as db:
        row = db.scalars(select(MailOutbox)).one()
    assert row.status == "failed"
    assert row.last_error == "auth_failed"


def test_smtp_password_is_encrypted_and_kept_when_left_blank(installed: Installed) -> None:
    from app.mail.schemas import MailSettingsInput

    base = {
        "host": "smtp.office365.com",
        "port": 587,
        "security": "starttls",
        "from_name": "ERP",
        "from_address": "erp@adeyemiholdings.ng",
    }
    with session_scope() as db:
        mail.save_settings(
            db, MailSettingsInput(**base, password="s3cret-smtp"), actor_id=None, actor_label="t"
        )
    with session_scope() as db:
        stored = db.scalars(select(MailSettings)).one().password_encrypted
        assert stored and "s3cret-smtp" not in stored
        mail.save_settings(
            db, MailSettingsInput(**base, password=""), actor_id=None, actor_label="t"
        )
    with session_scope() as db:
        assert db.scalars(select(MailSettings)).one().password_encrypted == stored
        assert mail.get_settings_view(db).has_password  # type: ignore[union-attr]
