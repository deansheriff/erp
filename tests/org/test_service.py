"""Structure services: validation, cycles refused on save, assignment rules, auditing."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.core import clock
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as hr
from app.modules.hr.schemas import PersonInput
from app.org import resolver
from app.org import service as org_service
from tests.authz.conftest import People
from tests.org.conftest import Org

pytestmark = pytest.mark.db
REASON = "Restructure approved by the board"


@pytest.fixture()
def admin(people: People, org: Org) -> Actor:
    user = people.add("Ngozi Admin")
    people.give(user, "organization_administrator")
    return people.actor(user)


def test_a_department_cant_move_under_its_own_sub_department(org: Org, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc:
        org_service.save_department(
            org.db,
            admin,
            org.departments["FIN"],
            code="FIN",
            name="Finance",
            parent_id=org.departments["TRE"],
            cost_centre_id=None,
        )
    assert "own sub-departments" in exc.value.field_errors["parent_id"]


def test_a_position_cant_report_to_its_own_report(org: Org, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc:
        org_service.save_position(
            org.db,
            admin,
            org.positions["Head of Finance"],
            title="Head of Finance",
            department_id=org.departments["FIN"],
            parent_id=org.positions["Accountant"],
            grade_id=None,
            branch_id=None,
        )
    assert "own reports" in exc.value.field_errors["parent_id"]


def test_moving_a_position_moves_its_team(org: Org, admin: Actor) -> None:
    org_service.save_position(
        org.db,
        admin,
        org.positions["Finance Manager"],
        title="Finance Manager",
        department_id=org.departments["FIN"],
        parent_id=org.positions["Head of HR"],
        grade_id=None,
        branch_id=None,
    )
    assert org.employees["Efe"] in resolver.reports_of(org.db, org.employees["Chi"])
    assert org.employees["Efe"] not in resolver.reports_of(org.db, org.employees["Bola"])


def test_codes_are_unique_and_tidy(org: Org, admin: Actor) -> None:
    with pytest.raises(ValidationFailed):
        org_service.save_department(
            org.db, admin, None, code="fin", name="Finance 2", parent_id=None, cost_centre_id=None
        )
    with pytest.raises(ValidationFailed):
        org_service.save_reference(org.db, admin, "branches", None, code="LAGOS HQ", name="Lagos")
    branch = org_service.save_reference(
        org.db, admin, "branches", None, code="lag-hq", name="Lagos HQ", state="Lagos"
    )
    assert [b.code for b in org_service.list_reference(org.db, "branches")] == ["LAG-HQ"]
    assert branch


def test_grades_need_a_rank(org: Org, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc:
        org_service.save_reference(org.db, admin, "grades", None, code="GL-12", name="Manager")
    assert "rank" in exc.value.field_errors
    org_service.save_reference(org.db, admin, "grades", None, code="GL-12", name="Manager", rank=12)


def test_in_use_records_cant_be_deactivated(org: Org, admin: Actor) -> None:
    with pytest.raises(Conflict, match="sub-departments"):
        org_service.set_department_active(org.db, admin, org.departments["FIN"], active=False)
    with pytest.raises(Conflict, match="report to it"):
        org_service.set_position_active(org.db, admin, org.positions["Head of HR"], active=False)
    with pytest.raises(Conflict, match="assignments"):
        org_service.set_position_active(org.db, admin, org.positions["HR Officer"], active=False)


def test_one_holder_of_each_kind_and_one_primary_position(org: Org, admin: Actor) -> None:
    today = clock.today()
    with pytest.raises(Conflict, match="already has a primary holder"):
        hr.assign_to_position(
            org.db,
            admin,
            position_id=org.positions["Accountant"],
            employee_id=org.employees["Hauwa"],
            kind="primary",
            start_date=today,
            end_date=None,
            reason=REASON,
        )
    with pytest.raises(Conflict, match="already has a primary position"):
        hr.assign_to_position(
            org.db,
            admin,
            position_id=org.positions["Finance Manager"],
            employee_id=org.employees["Efe"],
            kind="primary",
            start_date=today,
            end_date=None,
            reason=REASON,
        )
    # Hauwa can become the Finance Manager's primary holder alongside Gbenga's acting.
    hr.assign_to_position(
        org.db,
        admin,
        position_id=org.positions["Finance Manager"],
        employee_id=org.employees["Hauwa"],
        kind="primary",
        start_date=today,
        end_date=None,
        reason=REASON,
    )
    assert (
        resolver.holder(org.db, org.positions["Finance Manager"]).employee_id
        == org.employees["Hauwa"]
    )


def test_ending_and_cancelling_assignments(org: Org, admin: Actor) -> None:
    today = clock.today()
    future = hr.assign_to_position(
        org.db,
        admin,
        position_id=org.positions["Head of HR"],
        employee_id=org.employees["Hauwa"],
        kind="interim",
        start_date=today + timedelta(days=10),
        end_date=None,
        reason=REASON,
    )
    org_service.end_assignment(org.db, admin, future, last_day=today, reason=REASON)  # cancelled
    detail = org_service.position_detail(org.db, org.positions["Head of HR"])
    assert [h.kind for h in detail.history] == ["primary"]
    current = detail.history[0].assignment_id
    org_service.end_assignment(
        org.db, admin, current, last_day=today - timedelta(days=1), reason=REASON
    )
    assert resolver.holder(org.db, org.positions["Head of HR"]) is None
    with pytest.raises(Conflict, match="already ended"):
        org_service.end_assignment(org.db, admin, current, last_day=today, reason=REASON)


def test_assignments_need_the_sensitive_permission_and_a_reason(
    people: People, org: Org, admin: Actor
) -> None:
    clerk = people.actor(people.add("Plain Clerk"))
    with pytest.raises(PermissionDenied):
        hr.assign_to_position(
            org.db,
            clerk,
            position_id=org.positions["Head of HR"],
            employee_id=org.employees["Hauwa"],
            kind="acting",
            start_date=clock.today(),
            end_date=None,
            reason=REASON,
        )
    with pytest.raises(ValidationFailed):
        hr.assign_to_position(
            org.db,
            admin,
            position_id=org.positions["Head of HR"],
            employee_id=org.employees["Hauwa"],
            kind="acting",
            start_date=clock.today(),
            end_date=None,
            reason="",
        )


def test_changes_are_audited(org: Org, admin: Actor) -> None:
    org_service.save_department(
        org.db, admin, None, code="OPS", name="Operations", parent_id=None, cost_centre_id=None
    )
    actions = set(org.db.scalars(select(AuditLog.action).where(AuditLog.actor_id == admin.user_id)))
    assert "org.department.create" in actions


def test_people_can_be_added_and_linked_to_one_account(
    people: People, org: Org, admin: Actor
) -> None:
    user = people.add("New Joiner")
    pid = hr.add_person(
        org.db,
        admin,
        PersonInput(
            first_name="New",
            last_name="Joiner",
            work_email="NEW.joiner@adeyemiholdings.ng",
            user_id=user.id,
        ),
    )
    assert hr.get_person(org.db, admin, pid).work_email == "new.joiner@adeyemiholdings.ng"
    with pytest.raises(ValidationFailed) as exc:
        hr.add_person(
            org.db, admin, PersonInput(first_name="Other", last_name="Person", user_id=user.id)
        )
    assert "user_id" in exc.value.field_errors


def test_snapshots_from_a_changing_session_are_not_cached(org: Org) -> None:
    resolver.clear_cache()
    resolver.snapshot(org.db)  # this session bumped the version while building the fixture
    assert resolver._CACHE == {}
