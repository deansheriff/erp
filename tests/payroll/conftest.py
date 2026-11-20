"""Payroll fixtures: the leave staff (tests/leave) with payroll roles. Gbenga and Femi are
Payroll Officers (one prepares, the other reviews), Ada and Bola are Finance Managers
(approve, post), Chi is a Finance Officer (marks paid) and Dayo, an HR Officer, verifies
rent relief. Efe and Hauwa are paid on the Standard structure."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.modules.payroll import service, setup
from app.modules.payroll.models import PayComponent, PayGroup, PayrollRun, SalaryStructure
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.leave.conftest import staff  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401

STANDARD = {"basic": "200,000", "housing": "100,000", "transport": "50,000"}


@dataclass
class Payroll:
    hr: Hr
    group: PayGroup

    def component(self, code: str) -> PayComponent:
        return self.hr.db.scalars(select(PayComponent).where(PayComponent.code == code)).one()

    def pay(
        self, who: str, amounts: dict[str, str] | None = None, start: date = date(2026, 1, 1)
    ) -> None:
        structure = self.hr.db.scalars(
            select(SalaryStructure).where(SalaryStructure.name == "Standard")
        ).one()
        setup.set_pay(
            self.hr.db,
            self.hr.actor("Gbenga"),
            self.hr.employee(who),
            pay_group_id=self.group.id,
            structure_id=structure.id,
            start_date=start,
            amounts={
                str(self.component(code).id): value for code, value in (amounts or STANDARD).items()
            },
            reason="Starting pay",
        )

    def run(self, month: int, year: int = 2026) -> uuid.UUID:
        return service.create_run(
            self.hr.db, self.hr.actor("Gbenga"), pay_group_id=self.group.id, year=year, month=month
        )

    def through(self, run: uuid.UUID, last: str = "paid") -> None:
        """Take a run on from where it is to `last`, each step by a different person."""
        db, a = self.hr.db, self.hr.actor
        steps = [
            ("calculated", lambda: service.calculate(db, a("Gbenga"), run)),
            ("reviewed", lambda: service.review(db, a("Femi"), run)),
            ("approved", lambda: service.approve(db, a("Ada"), run)),
            ("posted", lambda: service.post(db, a("Bola"), run)),
            ("paid", lambda: service.mark_paid(db, a("Chi"), run)),
        ]
        done = [status for status, _ in steps]
        current = db.get(PayrollRun, run)
        assert current is not None
        start = done.index(current.status) + 1 if current.status in done else 0
        for status, step in steps[start:]:
            step()
            if status == last:
                break


def money(value: str) -> Decimal:
    return Decimal(value)


@pytest.fixture()
def payroll(staff: Hr) -> Payroll:  # noqa: F811
    db = staff.db
    for who, templates in (
        ("Gbenga", ["payroll_officer"]),
        ("Femi", ["payroll_officer"]),
        ("Ada", ["finance_manager"]),
        ("Bola", ["finance_manager"]),
        ("Chi", ["finance_officer"]),
    ):
        user = db.get(User, staff.org.users[who])
        assert user is not None
        for template in templates:
            staff.people.give(user, template)
    group = db.scalars(select(PayGroup).where(PayGroup.name == "Staff")).one()
    p = Payroll(staff, group)
    p.pay("Efe")
    p.pay("Hauwa", {"basic": "120,000", "housing": "30,000", "transport": "20,000"})
    return p
