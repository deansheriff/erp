"""Fleet fixtures. Chi is a Fleet Manager; Dayo is a licensed driver; Hauwa just works
here; Bola and Gbenga are Finance Managers. The 2026 financial year is open."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import clock
from app.identity.models import User
from app.modules.finance import periods
from app.modules.finance.models import Account
from app.modules.fleet import vehicles
from app.modules.fleet.vehicles import DriverInput, VehicleInput
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@dataclass
class Yard:
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

    def vehicle(
        self, registration: str = "LSD 482 KJ", *, driver: str = "", pool: bool = False
    ) -> uuid.UUID:
        return vehicles.save(
            self.db,
            self.actor("Chi"),
            None,
            VehicleInput(
                registration,
                "Toyota",
                "Hilux",
                "2021",
                "pickup",
                "diesel",
                "White",
                "",
                None,
                "1000",
                pool,
                str(self.employee(driver)) if driver else "",
                "",
            ),
        )


@pytest.fixture()
def yard(hr: Hr) -> Yard:  # noqa: F811
    db = hr.db
    for who, templates in (
        ("Chi", ("fleet_manager",)),
        ("Bola", ("finance_manager",)),
        ("Gbenga", ("finance_manager",)),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        for template in templates:
            hr.people.give(user, template)
    periods.open_year(db, hr.actor("Bola"), clock.today().year)
    vehicles.save_driver(
        db,
        hr.actor("Chi"),
        DriverInput(
            str(hr.employee("Dayo")), "LAG-123456", "E", clock.today() + timedelta(days=400), True
        ),
    )
    return Yard(hr)
