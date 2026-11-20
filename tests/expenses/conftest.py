"""Expense fixtures: the leave staff (tests/leave) plus a Finance cost centre. Efe's
managers are Gbenga (acting Finance Manager) then Bola; Ada also holds Finance Manager
(Finance approval and stepping in) and Hauwa is the Reimburser."""

from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import clock
from app.identity.models import User
from app.modules.expenses import service
from app.modules.expenses.models import ExpenseCategory
from app.org.models import CostCentre, Department
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.leave.conftest import staff  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200


@dataclass
class Team:
    hr: Hr
    cost_centre: object

    def category(self, name: str) -> ExpenseCategory:
        return self.hr.db.scalars(select(ExpenseCategory).where(ExpenseCategory.name == name)).one()

    def claim(
        self, who: str, *amounts: str, category: str = "Travel", title: str = "Trip"
    ) -> object:
        """A draft claim with one line per amount (each with a reason for no receipt)."""
        actor = self.hr.actor(who)
        claim = service.create(self.hr.db, actor, kind="claim", title=title)
        for i, amount in enumerate(amounts):
            service.save_line(
                self.hr.db,
                actor,
                claim,  # type: ignore[arg-type]
                None,
                day=clock.today(),
                category_id=self.category(category).id,
                description=f"Item {i + 1}",
                cost_centre_id=self.cost_centre,  # type: ignore[arg-type]
                amount=amount,
                no_receipt_reason="Lost it",
            )
        return claim


@pytest.fixture()
def team(staff: Hr) -> Team:  # noqa: F811
    db = staff.db
    for who, template in (("Ada", "finance_manager"), ("Hauwa", "reimburser")):
        user = db.get(User, staff.org.users[who])
        assert user is not None
        staff.people.give(user, template)
    centre = CostCentre(code="FIN01", name="Finance")
    db.add(centre)
    db.flush()
    finance = db.get(Department, staff.org.departments["FIN"])
    assert finance is not None
    finance.cost_centre_id = centre.id
    db.flush()
    return Team(staff, centre.id)


def money(value: str) -> Decimal:
    return Decimal(value)
