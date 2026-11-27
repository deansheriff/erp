"""Assistant fixtures: the fixture organisation (tests/org, tests/hr) with Ada as Super
Administrator, Chi as HR Manager and Bola as Line Manager (team: Efe, Femi, Gbenga). A
fake provider records each call and answers with a scripted reply; nothing reaches the
internet."""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.assistant import providers, setup
from app.modules.assistant.models import KINDS
from app.modules.assistant.providers import Reply
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401

KEY = "sk-test-0000000000000000wxyz"


@dataclass
class FakeProvider:
    """Answers with `reply(system, prompt, schema)`; keeps every call."""

    reply: Callable[[str, str, dict[str, Any]], Reply] = lambda *_: Reply(
        {"answer": "", "found": False, "citations": []}
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(self, *, system: str, prompt: str, schema: dict[str, Any], name: str) -> Reply:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "name": name})
        return self.reply(system, prompt, schema)


@dataclass
class Ai:
    hr: Hr
    fake: FakeProvider

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str) -> Actor:
        return self.hr.actor(who)

    def employee(self, who: str) -> uuid.UUID:
        return self.hr.employee(who)

    def answer(self, data: dict[str, Any] | None, *, refused: bool = False) -> None:
        self.fake.reply = lambda *_: Reply(data, 120, 80, refused)

    def enable(self, kinds: tuple[str, ...] = KINDS, provider: str = "anthropic") -> None:
        ada = self.actor("Ada")
        setup.save(
            self.db,
            ada,
            setup.SettingsInput(provider, "claude-opus-5-5", KEY, list(kinds), "200", "90"),
        )
        setup.turn_on(self.db, ada, accepted=True)


@pytest.fixture()
def ai(hr: Hr) -> Iterator[Ai]:  # noqa: F811
    user = hr.db.get(User, hr.org.users["Ada"])
    assert user is not None
    hr.people.give(user, "super_administrator")
    fake = FakeProvider()
    providers.set_provider(fake)
    try:
        yield Ai(hr, fake)
    finally:
        providers.set_provider(None)
