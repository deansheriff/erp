"""The policy engine against real Postgres: roles, time windows, scopes, department
restrictions, delegations and cache invalidation."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.engine import Resource, check
from app.authz.models import Delegation, Role, RoleGrant
from app.authz.permissions import Scope
from app.core import clock
from tests.authz.conftest import People

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("people")]


def _custom_role(db: Session, name: str, grants: dict[str, Scope]) -> Role:
    role = Role(name=name)
    db.add(role)
    db.flush()
    db.add_all(
        RoleGrant(role_id=role.id, permission_key=k, scope=s.value) for k, s in grants.items()
    )
    db.flush()
    engine.bump_version(db)
    return role


def _give_custom(people: People, user_id: uuid.UUID, role: Role, **kwargs: object) -> None:
    from app.authz.models import RoleAssignment

    people.db.add(
        RoleAssignment(
            user_id=user_id,
            role_id=role.id,
            valid_from=clock.now() - timedelta(minutes=1),
            reason="test fixture",
            **kwargs,
        )
    )
    people.db.flush()
    engine.bump_version(people.db)


def test_nobody_holds_anything_without_a_role(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    decision = check(db, user.id, "identity.user.read")
    assert not decision
    assert "no grant" in decision.reason


def test_unknown_permissions_are_a_programming_error(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    with pytest.raises(KeyError):
        check(db, user.id, "identity.user.fly")


def test_super_administrator_holds_every_permission(people: People, db: Session) -> None:
    from app.authz import catalogue

    user = people.add("Fatima Bello")
    people.give(user, "super_administrator")
    assert all(check(db, user.id, key) for key in catalogue.permissions())
    assert engine.load_grants(db, user.id).requires_mfa


def test_organisation_administrator_has_everything_but_technical_settings(
    people: People, db: Session
) -> None:
    user = people.add("Ngozi Adebayo")
    people.give(user, "organization_administrator")
    assert check(db, user.id, "identity.user.manage")
    assert check(db, user.id, "authz.role.manage")
    assert check(db, user.id, "audit.log.read")
    for key in ("system.mail.manage", "system.branding.manage", "system.security.manage"):
        assert not check(db, user.id, key), key


def test_auditor_reads_everything_and_changes_nothing(people: People, db: Session) -> None:
    user = people.add("Kemi Ade")
    people.give(user, "auditor")
    assert check(db, user.id, "audit.log.read")
    assert check(db, user.id, "identity.user.read")
    assert not check(db, user.id, "identity.user.manage")
    assert not check(db, user.id, "authz.role.manage")


@pytest.mark.parametrize(
    ("starts", "ends", "ended", "active"),
    [
        (timedelta(days=-1), None, False, True),
        (timedelta(days=1), None, False, False),  # acting role that hasn't started
        (timedelta(days=-10), timedelta(days=-1), False, False),  # acting role that ended
        (timedelta(days=-1), None, True, False),  # removed early
    ],
)
def test_role_assignments_only_count_inside_their_period(
    people: People,
    db: Session,
    starts: timedelta,
    ends: timedelta | None,
    ended: bool,
    active: bool,
) -> None:
    user = people.add("Tunde Bakare")
    now = clock.now()
    assignment = people.give(
        user, "auditor", starts=now + starts, ends=now + ends if ends else None
    )
    if ended:
        assignment.ended_at = now
        engine.bump_version(db)
    assert bool(check(db, user.id, "audit.log.read")) is active


def test_next_change_is_the_soonest_time_based_change(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    now = clock.now()
    people.give(user, "auditor", ends=now + timedelta(days=3))
    people.give(user, "organization_administrator", starts=now + timedelta(days=1))
    grants = engine.load_grants(db, user.id)
    assert grants.next_change is not None
    assert abs((grants.next_change - (now + timedelta(days=1))).total_seconds()) < 5


class FakeOrg:
    """Okafor manages Eze; both are in Finance, which contains Payables."""

    def __init__(
        self,
        people: dict[uuid.UUID, uuid.UUID],
        team: dict[uuid.UUID, set[uuid.UUID]],
        departments: dict[uuid.UUID, set[uuid.UUID]],
        tree: dict[uuid.UUID, set[uuid.UUID]],
    ) -> None:
        self.people, self.team, self.departments, self.tree = people, team, departments, tree

    def employee_of(self, db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
        return self.people.get(user_id)

    def team_employees(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        return self.team.get(user_id, set())

    def departments_of(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        return self.departments.get(user_id, set())

    def departments_under(self, db: Session, department_id: uuid.UUID) -> set[uuid.UUID]:
        return self.tree.get(department_id, {department_id})


@pytest.fixture()
def org(people: People) -> dict[str, uuid.UUID]:
    manager, report = people.add("Chinedu Okafor"), people.add("Amaka Eze")
    ids = {
        "manager": manager.id,
        "report": report.id,
        "manager_emp": uuid.uuid4(),
        "report_emp": uuid.uuid4(),
        "stranger_emp": uuid.uuid4(),
        "finance": uuid.uuid4(),
        "payables": uuid.uuid4(),
        "sales": uuid.uuid4(),
    }
    engine.set_org_resolver(
        FakeOrg(
            people={manager.id: ids["manager_emp"], report.id: ids["report_emp"]},
            team={manager.id: {ids["report_emp"]}},
            departments={manager.id: {ids["finance"]}, report.id: {ids["payables"]}},
            tree={ids["finance"]: {ids["finance"], ids["payables"]}},
        )
    )
    return ids


@pytest.mark.parametrize(
    ("scope", "own", "report", "department_member", "stranger"),
    [
        (Scope.SELF, True, False, False, False),
        (Scope.TEAM, True, True, False, False),
        (Scope.DEPARTMENT, True, True, True, False),
        (Scope.ORGANIZATION, True, True, True, True),
    ],
)
def test_scopes_reach_progressively_further(
    people: People,
    db: Session,
    org: dict[str, uuid.UUID],
    scope: Scope,
    own: bool,
    report: bool,
    department_member: bool,
    stranger: bool,
) -> None:
    role = _custom_role(db, f"Viewer {scope}", {"identity.user.read": scope})
    _give_custom(people, org["manager"], role)
    cases = {
        "own": Resource(owner_employee_id=org["manager_emp"], department_id=org["finance"]),
        "report": Resource(owner_employee_id=org["report_emp"], department_id=org["payables"]),
        "department_member": Resource(
            owner_employee_id=uuid.uuid4(), department_id=org["payables"]
        ),
        "stranger": Resource(owner_employee_id=org["stranger_emp"], department_id=org["sales"]),
    }
    expected = {
        "own": own,
        "report": report,
        "department_member": department_member,
        "stranger": stranger,
    }
    for name, resource in cases.items():
        assert bool(check(db, org["manager"], "identity.user.read", resource)) is expected[name], (
            name
        )


def test_department_restriction_limits_even_organisation_scope(
    people: People, db: Session, org: dict[str, uuid.UUID]
) -> None:
    role = _custom_role(db, "Payables HR", {"identity.user.read": Scope.ORGANIZATION})
    _give_custom(people, org["manager"], role, department_id=org["payables"])
    inside = Resource(department_id=org["payables"])
    outside = Resource(department_id=org["sales"])
    nowhere = Resource()
    assert check(db, org["manager"], "identity.user.read", inside)
    assert not check(db, org["manager"], "identity.user.read", outside)
    assert not check(db, org["manager"], "identity.user.read", nowhere)


def _delegate(
    db: Session, frm: uuid.UUID, to: uuid.UUID, key: str, scope: Scope, **overrides: object
) -> Delegation:
    now = clock.now()
    values: dict[str, object] = {
        "from_user_id": frm,
        "to_user_id": to,
        "permission_key": key,
        "scope": scope.value,
        "starts_at": now - timedelta(hours=1),
        "ends_at": now + timedelta(days=5),
        "reason": "annual leave",
        "created_by": frm,
    }
    values.update(overrides)
    d = Delegation(**values)
    db.add(d)
    db.flush()
    engine.bump_version(db)
    return d


def test_a_delegation_lends_a_permission_for_its_period(people: People, db: Session) -> None:
    boss, deputy = people.add("Chinedu Okafor"), people.add("Amaka Eze")
    people.give(boss, "auditor")
    d = _delegate(db, boss.id, deputy.id, "audit.log.read", Scope.ORGANIZATION)
    assert check(db, deputy.id, "audit.log.read")
    grant = engine.load_grants(db, deputy.id).by_permission["audit.log.read"][0]
    assert grant.on_behalf_of == boss.id
    d.revoked_at = clock.now()
    engine.bump_version(db)
    assert not check(db, deputy.id, "audit.log.read")


def test_expired_and_future_delegations_lend_nothing(people: People, db: Session) -> None:
    boss, deputy = people.add("Chinedu Okafor"), people.add("Amaka Eze")
    people.give(boss, "auditor")
    now = clock.now()
    _delegate(
        db,
        boss.id,
        deputy.id,
        "audit.log.read",
        Scope.ORGANIZATION,
        starts_at=now - timedelta(days=5),
        ends_at=now - timedelta(days=1),
    )
    _delegate(
        db,
        boss.id,
        deputy.id,
        "identity.user.read",
        Scope.ORGANIZATION,
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=2),
    )
    assert not check(db, deputy.id, "audit.log.read")
    assert not check(db, deputy.id, "identity.user.read")


def test_a_delegation_carries_only_what_the_delegator_still_holds(
    people: People, db: Session, org: dict[str, uuid.UUID]
) -> None:
    role = _custom_role(db, "Team viewer", {"identity.user.read": Scope.TEAM})
    _give_custom(people, org["manager"], role)
    deputy = people.add("Bola Ige")
    # Asked for organisation scope, but the delegator only holds team scope.
    _delegate(db, org["manager"], deputy.id, "identity.user.read", Scope.ORGANIZATION)
    assert engine.load_grants(db, deputy.id).widest("identity.user.read") is Scope.TEAM
    # Scope is seen from the delegator: the deputy sees the manager's team, not their own.
    report_record = Resource(owner_employee_id=org["report_emp"])
    assert check(db, deputy.id, "identity.user.read", report_record)
    # When the delegator loses the role, the delegation carries nothing.
    for g in db.scalars(select(RoleGrant).where(RoleGrant.role_id == role.id)):
        db.delete(g)
    db.flush()
    engine.bump_version(db)
    assert not check(db, deputy.id, "identity.user.read")


def test_delegated_permissions_are_not_passed_on_again(people: People, db: Session) -> None:
    boss, deputy, third = (
        people.add("Chinedu Okafor"),
        people.add("Amaka Eze"),
        people.add("Bola Ige"),
    )
    people.give(boss, "auditor")
    _delegate(db, boss.id, deputy.id, "audit.log.read", Scope.ORGANIZATION)
    _delegate(db, deputy.id, third.id, "audit.log.read", Scope.ORGANIZATION)
    assert check(db, deputy.id, "audit.log.read")
    assert not check(db, third.id, "audit.log.read")


def test_request_cache_is_keyed_on_the_authz_version(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    engine.begin_request_cache()
    assert not check(db, user.id, "audit.log.read")
    people.give(user, "auditor")  # bumps the version
    assert check(db, user.id, "audit.log.read")


# ------------------------------------------------------------------ scope_filter

_table = Table(
    "records",
    MetaData(),
    Column("owner_user_id", Uuid),
    Column("owner_employee_id", Uuid),
    Column("department_id", Uuid),
)


class Record:
    __authz__ = engine.AuthzColumns(
        owner_user=_table.c.owner_user_id,  # type: ignore[arg-type]
        owner_employee=_table.c.owner_employee_id,  # type: ignore[arg-type]
        department=_table.c.department_id,  # type: ignore[arg-type]
    )


def _sql(clause: object) -> str:
    return str(
        clause.compile(
            dialect=postgresql.dialect(),  # type: ignore[attr-defined]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_scope_filter_is_false_without_a_grant(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    assert _sql(engine.scope_filter(db, user.id, "identity.user.read", Record)) == "false"


def test_scope_filter_is_true_at_organisation_scope(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    people.give(user, "auditor")
    assert _sql(engine.scope_filter(db, user.id, "identity.user.read", Record)) == "true"


def test_scope_filter_limits_self_scope_to_own_rows(people: People, db: Session) -> None:
    user = people.add("Tunde Bakare")
    _give_custom(people, user.id, _custom_role(db, "Self", {"identity.user.read": Scope.SELF}))
    sql = _sql(engine.scope_filter(db, user.id, "identity.user.read", Record))
    assert "owner_user_id" in sql and str(user.id) in sql


def test_scope_filter_uses_the_org_chart_for_team_and_department(
    people: People, db: Session, org: dict[str, uuid.UUID]
) -> None:
    role = _custom_role(db, "Dept", {"identity.user.read": Scope.DEPARTMENT})
    _give_custom(people, org["manager"], role)
    sql = _sql(engine.scope_filter(db, org["manager"], "identity.user.read", Record))
    assert "department_id IN" in sql
    assert str(org["payables"]) in sql and str(org["sales"]) not in sql
