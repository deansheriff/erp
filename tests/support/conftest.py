"""Support fixtures. Ada administers; the IT team is led by Chi with Dayo as agent, and
the HR team has Efe; Hauwa raises tickets. The clock can be moved (Lagos time)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from app.authz.models import RoleAssignment
from app.core import clock
from app.identity.models import User
from app.modules.support import setup
from app.modules.support.models import TicketCategory
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401

LAGOS = ZoneInfo("Africa/Lagos")


@dataclass
class Desk:
    hr: Hr
    it: uuid.UUID
    hr_team: uuid.UUID
    set_time: Callable[[datetime], None]

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str):  # type: ignore[no-untyped-def]
        return self.hr.actor(who)

    def employee(self, who: str) -> uuid.UUID:
        return self.hr.employee(who)

    def category(self, name: str) -> str:
        return str(
            self.db.scalars(select(TicketCategory.id).where(TicketCategory.name == name)).one()
        )


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LAGOS)


@pytest.fixture()
def desk(hr: Hr, monkeypatch: pytest.MonkeyPatch) -> Desk:  # noqa: F811
    db = hr.db
    for who, templates in (
        ("Ada", ("organization_administrator",)),
        ("Chi", ("support_agent",)),
        ("Dayo", ("support_agent",)),
        ("Efe", ("support_agent",)),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        for template in templates:
            hr.people.give(user, template)
    # Everyone's roles start before the moved clock's Monday, so they're in force then.
    db.execute(update(RoleAssignment).values(valid_from=datetime(2026, 1, 1, tzinfo=UTC)))
    db.flush()
    moment = {"now": at(2026, 11, 23, 9)}  # a Monday morning
    monkeypatch.setattr(clock, "now", lambda: moment["now"].astimezone(UTC))

    def set_time(value: datetime) -> None:
        moment["now"] = value

    ada = hr.actor("Ada")
    it = setup.save_team(
        db,
        ada,
        None,
        setup.TeamInput("IT", str(hr.employee("Chi")), [str(hr.employee("Dayo"))], True),
    )
    hr_team = setup.save_team(
        db, ada, None, setup.TeamInput("HR", "", [str(hr.employee("Efe"))], True)
    )
    setup.save_category(db, ada, None, name="Network", team_id=str(it), active=True)
    setup.save_category(db, ada, None, name="Payroll query", team_id=str(hr_team), active=True)
    return Desk(hr, it, hr_team, set_time)
