"""A fixture organisation (brief section 4.3): three departments (one with a
sub-department), a four-level position tree, a vacancy filled by an acting holder.

    Managing Director (EXE)            Ada
    ├── Head of Finance (FIN)          Bola
    │   └── Finance Manager (FIN)      vacant; Gbenga acting
    │       ├── Accountant (FIN)       Efe
    │       ├── Payables Clerk (FIN)   Gbenga
    │       └── Treasury Analyst (TRE) Femi
    └── Head of HR (HR)                Chi
        └── HR Officer (HR)            Dayo

Departments: Executive (EXE), Finance (FIN) with Treasury (TRE) inside it, Human Resources (HR).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core import clock
from app.modules.hr.models import Employee
from app.org import resolver
from app.org.models import Department, Position, PositionAssignment
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixture)


@dataclass
class Org:
    db: Session
    departments: dict[str, uuid.UUID] = field(default_factory=dict)
    positions: dict[str, uuid.UUID] = field(default_factory=dict)
    employees: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)

    def assign(
        self,
        who: str,
        position: str,
        kind: str = "primary",
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> PositionAssignment:
        row = PositionAssignment(
            position_id=self.positions[position],
            employee_id=self.employees[who],
            kind=kind,
            start_date=start or clock.today() - timedelta(days=30),
            end_date=end,
            reason="fixture",
        )
        self.db.add(row)
        self.changed()
        return row

    def changed(self) -> None:
        self.db.flush()
        resolver.bump_version(self.db)


@pytest.fixture()
def org(people: People, db: Session) -> Org:  # noqa: F811
    o = Org(db)
    for code, name, parent in [
        ("EXE", "Executive", None),
        ("FIN", "Finance", None),
        ("TRE", "Treasury", "FIN"),
        ("HR", "Human Resources", None),
    ]:
        d = Department(
            code=code, name=name, parent_id=o.departments.get(parent) if parent else None
        )
        db.add(d)
        db.flush()
        o.departments[code] = d.id
    for title, dept, parent in [
        ("Managing Director", "EXE", None),
        ("Head of Finance", "FIN", "Managing Director"),
        ("Finance Manager", "FIN", "Head of Finance"),
        ("Accountant", "FIN", "Finance Manager"),
        ("Payables Clerk", "FIN", "Finance Manager"),
        ("Treasury Analyst", "TRE", "Finance Manager"),
        ("Head of HR", "HR", "Managing Director"),
        ("HR Officer", "HR", "Head of HR"),
    ]:
        p = Position(
            title=title,
            department_id=o.departments[dept],
            parent_id=o.positions.get(parent) if parent else None,
        )
        db.add(p)
        db.flush()
        o.positions[title] = p.id
    for name in ["Ada", "Bola", "Chi", "Dayo", "Efe", "Femi", "Gbenga", "Hauwa"]:
        user = people.add(f"{name} Test")
        e = Employee(
            first_name=name,
            last_name="Test",
            user_id=user.id,
            work_email=f"{name.lower()}@adeyemiholdings.ng",
        )
        db.add(e)
        db.flush()
        o.employees[name], o.users[name] = e.id, user.id
    for who, position in [
        ("Ada", "Managing Director"),
        ("Bola", "Head of Finance"),
        ("Chi", "Head of HR"),
        ("Dayo", "HR Officer"),
        ("Efe", "Accountant"),
        ("Femi", "Treasury Analyst"),
        ("Gbenga", "Payables Clerk"),
    ]:
        o.assign(who, position)
    o.assign("Gbenga", "Finance Manager", "acting")
    return o
