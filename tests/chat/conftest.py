"""Chat fixtures: the fixture organisation (tests/org) with everyone given the Employee
role, which carries chat. Ada is an Organisation Administrator; everyone else is plain.

Department channels: Executive (Ada), Finance (Bola head; Efe, Gbenga, and Femi from
Treasury below it), Treasury (Femi head), Human Resources (Chi head; Dayo). Hauwa holds no
position, so she is in no channel."""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.chat.models import Conversation
from app.realtime import broker as realtime
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


class RecordingBroker(realtime.MemoryBroker):
    """Keeps what was published, by channel."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    def publish(self, channel: str, message: str) -> None:
        self.sent.append((channel, message))
        super().publish(channel, message)


@dataclass
class Chat:
    hr: Hr
    broker: RecordingBroker

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str) -> Actor:
        return self.hr.actor(who)

    def user(self, who: str) -> uuid.UUID:
        return self.hr.org.users[who]

    def channel(self, code: str) -> uuid.UUID:
        department = self.hr.org.departments[code]
        return self.db.scalars(
            select(Conversation.id).where(Conversation.department_id == department)
        ).one()


@pytest.fixture()
def chat(hr: Hr) -> Chat:  # noqa: F811
    user = hr.db.get(User, hr.org.users["Ada"])
    assert user is not None
    hr.people.give(user, "organization_administrator")
    broker = RecordingBroker()
    realtime.set_broker(broker)
    try:
        yield Chat(hr, broker)  # type: ignore[misc]
    finally:
        realtime.set_broker(None)
