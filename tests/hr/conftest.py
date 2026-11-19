"""HR fixtures: the fixture organisation from tests/org with role templates handed out:

Ada (Managing Director)  Employee
Bola (Head of Finance)   Employee + Line Manager (team: Efe, Femi, Gbenga)
Chi (Head of HR)         Employee + HR Manager
Dayo (HR Officer)        Employee + HR Officer
Efe (Accountant)         Employee
Hauwa (no position)      Employee + IT Support
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import checklists
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixture)
from tests.files.conftest import scanner  # noqa: F401  (re-exported fixture)
from tests.org.conftest import Org, org  # noqa: F401  (re-exported fixture)

ROLES = {
    "Bola": ["line_manager"],
    "Chi": ["hr_manager"],
    "Dayo": ["hr_officer"],
    "Hauwa": ["it_support"],
}


@dataclass
class Hr:
    db: Session
    org: Org
    people: People

    def actor(self, who: str) -> Actor:
        user = self.db.get(User, self.org.users[who])
        assert user is not None
        return self.people.actor(user)

    def employee(self, who: str) -> uuid.UUID:
        return self.org.employees[who]


@pytest.fixture()
def hr(org: Org, people: People, db: Session) -> Hr:  # noqa: F811
    for who, user_id in org.users.items():
        user = db.get(User, user_id)
        assert user is not None
        people.give(user, "employee")
        for template in ROLES.get(who, []):
            people.give(user, template)
    return Hr(db, org, people)


def add_templates(db: Session, hr: Hr) -> None:
    """A default onboarding and offboarding template (clean_db empties the seeded ones)."""
    for kind, tasks in (
        (
            "onboarding",
            [("Laptop", "it", "-1"), ("Welcome", "manager", "0"), ("Profile", "employee", "3")],
        ),
        ("offboarding", [("Return laptop", "employee", "0"), ("Exit interview", "hr", "-2")]),
    ):
        checklists.save_template(
            db,
            hr.actor("Chi"),
            None,
            kind=kind,
            name=f"Default {kind}",
            department_id=None,
            position_id=None,
            is_active=True,
            tasks=tasks,
        )
