"""Attendance fixtures: the leave staff (tests/leave) with a Lagos branch, the seeded Day
shift (Monday to Friday, 08:00 to 17:00, an hour's break, 15 minutes' grace) for Efe and
Femi, and a clock the test moves. Gbenga (acting Finance Manager) is Efe's manager; Bola is Line
Manager over Efe, Femi and Gbenga; Chi is HR Manager and Dayo HR Officer."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.authz import engine
from app.authz.models import RoleAssignment
from app.core import clock
from app.modules.attendance import settings
from app.modules.attendance.models import ShiftPattern
from app.modules.hr.models import Employee
from app.org.models import Branch
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.leave.conftest import staff  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401

LAGOS = ZoneInfo("Africa/Lagos")
MONDAY = date(2026, 11, 16)
HEAD_OFFICE = (6.428100, 3.421900)


def lagos(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LAGOS)


@dataclass
class Crew:
    hr: Hr
    branch_id: object
    day_shift: ShiftPattern
    set_time: Callable[[datetime], None]


@pytest.fixture()
def crew(staff: Hr, monkeypatch: pytest.MonkeyPatch) -> Crew:  # noqa: F811
    db = staff.db
    # The test clock is in the past: let the fixture's roles start before it.
    for assignment in db.scalars(select(RoleAssignment)):
        assignment.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    engine.bump_version(db)
    moment = {"now": lagos(MONDAY, 7, 0)}
    monkeypatch.setattr(clock, "now", lambda: moment["now"].astimezone(UTC))

    def set_time(value: datetime) -> None:
        moment["now"] = value

    branch = Branch(code="LOS", name="Lagos Head Office")
    db.add(branch)
    db.flush()
    for who in ("Efe", "Femi"):
        row = db.get(Employee, staff.employee(who))
        assert row is not None
        row.branch_id = branch.id
    day_shift = db.scalars(select(ShiftPattern).where(ShiftPattern.name == "Day shift")).one()
    for who in ("Efe", "Femi"):
        settings.assign(
            db,
            staff.actor("Chi"),
            staff.employee(who),
            pattern_id=day_shift.id,
            start=date(2026, 11, 2),
            end=None,
        )
    db.flush()
    return Crew(staff, branch.id, day_shift, set_time)
