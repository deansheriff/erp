"""Leave fixtures: the HR fixture organisation (tests/hr) with hire dates, so service-based
entitlements apply. Bola is Efe's, Femi's and Gbenga's line manager (Gbenga acting as
Finance Manager is Efe's direct manager); Chi is HR Manager and Dayo HR Officer."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.hr.models import Employee
from app.modules.leave.models import LeaveType
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.files.conftest import scanner  # noqa: F401
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@pytest.fixture()
def staff(hr: Hr) -> Hr:  # noqa: F811
    """Everyone hired in 2020; the other people who manage someone (Ada, Gbenga) also
    hold Line Manager."""
    from app.identity.models import User

    for row in hr.db.scalars(select(Employee)):
        row.hire_date = date(2020, 1, 6)
    for who in ("Ada", "Gbenga"):
        user = hr.db.get(User, hr.org.users[who])
        assert user is not None
        hr.people.give(user, "line_manager")
    hr.db.flush()
    return hr


def leave_type(db: Session, code: str) -> LeaveType:
    return db.scalars(select(LeaveType).where(LeaveType.code == code)).one()
