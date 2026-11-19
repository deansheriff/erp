"""Scopes through the real position tree (brief section 4.3): exactly which people each
scope can see in the People list, and the matching record-level checks."""

import pytest
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.engine import Resource
from app.authz.models import Role, RoleAssignment, RoleGrant
from app.authz.permissions import Scope
from app.core import clock
from app.modules.hr import service as hr
from app.org import resolver
from tests.authz.conftest import People
from tests.org.conftest import Org

pytestmark = pytest.mark.db

EVERYONE = ["Ada", "Bola", "Chi", "Dayo", "Efe", "Femi", "Gbenga", "Hauwa"]


def _grant(db: Session, org: Org, who: str, scope: Scope) -> None:
    role = Role(name=f"People viewer ({scope}) {who}")
    db.add(role)
    db.flush()
    db.add(RoleGrant(role_id=role.id, permission_key="hr.employee.read", scope=scope.value))
    db.add(
        RoleAssignment(
            user_id=org.users[who], role_id=role.id, valid_from=clock.now(), reason="fixture"
        )
    )
    db.flush()
    engine.bump_version(db)


def _visible(db: Session, people: People, org: Org, who: str) -> list[str]:
    from app.identity.models import User

    user = db.get(User, org.users[who])
    assert user is not None
    rows = hr.list_people(db, people.actor(user))
    return sorted(r.first_name for r in rows)


@pytest.mark.parametrize(
    ("who", "scope", "expected"),
    [
        ("Efe", Scope.SELF, ["Efe"]),
        ("Bola", Scope.TEAM, ["Bola", "Efe", "Femi", "Gbenga"]),
        ("Gbenga", Scope.TEAM, ["Efe", "Femi", "Gbenga"]),  # acting manager sees the team
        ("Chi", Scope.TEAM, ["Chi", "Dayo"]),
        ("Ada", Scope.TEAM, ["Ada", "Bola", "Chi", "Dayo", "Efe", "Femi", "Gbenga"]),
        ("Bola", Scope.DEPARTMENT, ["Bola", "Efe", "Femi", "Gbenga"]),  # Finance + Treasury
        ("Femi", Scope.DEPARTMENT, ["Femi"]),  # Treasury only
        ("Dayo", Scope.DEPARTMENT, ["Chi", "Dayo"]),
        ("Efe", Scope.ORGANIZATION, EVERYONE),
        ("Hauwa", Scope.TEAM, ["Hauwa"]),  # no position: only themselves
    ],
)
def test_people_list_follows_scope(
    people: People, org: Org, who: str, scope: Scope, expected: list[str]
) -> None:
    _grant(org.db, org, who, scope)
    assert _visible(org.db, people, org, who) == expected


def test_without_the_permission_nobody_is_listed(people: People, org: Org) -> None:
    assert _visible(org.db, people, org, "Bola") == []


def test_team_scope_changes_with_the_chart(people: People, org: Org) -> None:
    _grant(org.db, org, "Gbenga", Scope.TEAM)
    org.assign("Hauwa", "Finance Manager", "primary")  # the primary holder takes over
    assert _visible(org.db, people, org, "Gbenga") == ["Gbenga"]


def test_record_checks_match_the_list(people: People, org: Org) -> None:
    _grant(org.db, org, "Bola", Scope.TEAM)
    db, bola = org.db, org.users["Bola"]
    assert engine.check(
        db, bola, "hr.employee.read", Resource(owner_employee_id=org.employees["Femi"])
    )
    assert not engine.check(
        db, bola, "hr.employee.read", Resource(owner_employee_id=org.employees["Dayo"])
    )


def test_department_scope_record_check_uses_positions(people: People, org: Org) -> None:
    _grant(org.db, org, "Chi", Scope.DEPARTMENT)
    db, chi = org.db, org.users["Chi"]
    assert engine.check(
        db, chi, "hr.employee.read", Resource(owner_employee_id=org.employees["Dayo"])
    )
    assert not engine.check(
        db, chi, "hr.employee.read", Resource(owner_employee_id=org.employees["Efe"])
    )


def test_department_restricted_roles_reach_only_that_department(people: People, org: Org) -> None:
    db = org.db
    role = Role(name="Treasury people viewer")
    db.add(role)
    db.flush()
    db.add(RoleGrant(role_id=role.id, permission_key="hr.employee.read", scope="organization"))
    db.add(
        RoleAssignment(
            user_id=org.users["Ada"],
            role_id=role.id,
            valid_from=clock.now(),
            reason="fixture",
            department_id=org.departments["FIN"],
        )
    )
    db.flush()
    engine.bump_version(db)
    resolver.clear_cache()
    # Restricted to Finance (and Treasury below it): records with a department only.
    assert engine.check(
        db, org.users["Ada"], "hr.employee.read", Resource(department_id=org.departments["TRE"])
    )
    assert not engine.check(
        db, org.users["Ada"], "hr.employee.read", Resource(department_id=org.departments["HR"])
    )
