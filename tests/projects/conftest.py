"""Project fixtures. Chi (Head of HR) is a Project Manager and Dayo's line manager; Ada
(Managing Director, Chi's manager) is a Director and Line Manager; Bola and Gbenga are
Finance Managers; Dayo and Efe are staff. The 2026 financial year is open."""

import uuid
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.modules.finance import periods
from app.modules.finance.models import Account
from app.modules.projects import projects
from app.modules.projects.projects import ProjectInput
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@dataclass
class Work:
    hr: Hr

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str):  # type: ignore[no-untyped-def]
        return self.hr.actor(who)

    def employee(self, who: str) -> uuid.UUID:
        return self.hr.employee(who)

    def account(self, code: str) -> Account:
        return self.db.scalars(select(Account).where(Account.code == code)).one()

    def project(
        self,
        name: str = "Ikeja warehouse fit-out",
        *,
        members: tuple[str, ...] = ("Dayo", "Efe"),
        budget: dict[str, str] | None = None,
        start: bool = True,
    ) -> uuid.UUID:
        """Chi sets it up and starts it."""
        row = projects.save(
            self.db,
            self.actor("Chi"),
            None,
            ProjectInput(
                name,
                "",
                str(self.employee("Chi")),
                date(2026, 10, 1),
                date(2026, 12, 31),
                "",
                "",
                budget or {"labour": "100,000", "materials": "250,000"},
                [str(self.employee(m)) for m in members],
                [("Design sign-off", date(2026, 10, 15))],
            ),
        )
        if start:
            projects.move(self.db, self.actor("Chi"), row, "active")
        return row


@pytest.fixture()
def work(hr: Hr) -> Work:  # noqa: F811
    db = hr.db
    for who, templates in (
        ("Chi", ("project_manager", "line_manager")),
        ("Ada", ("director", "line_manager")),
        ("Bola", ("finance_manager",)),
        ("Gbenga", ("finance_manager",)),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        for template in templates:
            hr.people.give(user, template)
    periods.open_year(db, hr.actor("Bola"), 2026)
    return Work(hr)
