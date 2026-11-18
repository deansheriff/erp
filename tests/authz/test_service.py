"""Managing roles, assignments and delegations: the rules that stop people raising their
own access, the last-super-admin guard, validation and auditing."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.authz import engine
from app.authz import service as authz
from app.authz.permissions import Scope
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity import service as identity
from tests.authz.conftest import People

pytestmark = pytest.mark.db

REASON = "Covering December payroll close"


@pytest.fixture()
def admins(people: People) -> dict[str, object]:
    root, org_admin, staff = (
        people.add("Fatima Bello"),
        people.add("Ngozi Adebayo"),
        people.add("Tunde Bakare"),
    )
    people.give(root, "super_administrator")
    people.give(org_admin, "organization_administrator")
    return {"root": root, "org_admin": org_admin, "staff": staff}


# ------------------------------------------------------------------ assignments


def test_assigning_a_role_bumps_the_version_and_is_audited(
    people: People, db: Session, admins: dict
) -> None:
    before = engine.authz_version(db)
    authz.assign_role(
        db,
        people.actor(admins["org_admin"]),
        user_id=admins["staff"].id,
        role_id=people.role("auditor").id,
        valid_from=None,
        valid_to=None,
        reason=REASON,
    )
    assert engine.authz_version(db) > before
    assert engine.check(db, admins["staff"].id, "audit.log.read")
    event = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "authz.assignment.create")
        .order_by(AuditLog.occurred_at.desc())
    ).first()
    assert event is not None and event.reason == REASON
    assert event.changes["role"]["to"] == "Auditor"


def test_nobody_can_change_their_own_roles(people: People, db: Session, admins: dict) -> None:
    with pytest.raises(PermissionDenied, match="your own roles"):
        authz.assign_role(
            db,
            people.actor(admins["org_admin"]),
            user_id=admins["org_admin"].id,
            role_id=people.role("auditor").id,
            valid_from=None,
            valid_to=None,
            reason=REASON,
        )


def test_assigning_needs_the_assignment_permission(
    people: People, db: Session, admins: dict
) -> None:
    other = people.add("Bola Ige")
    with pytest.raises(PermissionDenied):
        authz.assign_role(
            db,
            people.actor(admins["staff"]),
            user_id=other.id,
            role_id=people.role("auditor").id,
            valid_from=None,
            valid_to=None,
            reason=REASON,
        )


def test_organisation_admins_cannot_create_super_admins(
    people: People, db: Session, admins: dict
) -> None:
    with pytest.raises(PermissionDenied, match="permissions you don't hold"):
        authz.assign_role(
            db,
            people.actor(admins["org_admin"]),
            user_id=admins["staff"].id,
            role_id=people.role("super_administrator").id,
            valid_from=None,
            valid_to=None,
            reason=REASON,
        )


def test_super_admins_can_create_super_admins(people: People, db: Session, admins: dict) -> None:
    authz.assign_role(
        db,
        people.actor(admins["root"]),
        user_id=admins["staff"].id,
        role_id=people.role("super_administrator").id,
        valid_from=None,
        valid_to=None,
        reason=REASON,
    )
    assert authz.is_super_admin(db, admins["staff"].id)


def test_assignments_need_a_reason_and_a_sensible_period(
    people: People, db: Session, admins: dict
) -> None:
    actor = people.actor(admins["org_admin"])
    role_id = people.role("auditor").id
    with pytest.raises(ValidationFailed) as exc:
        authz.assign_role(
            db,
            actor,
            user_id=admins["staff"].id,
            role_id=role_id,
            valid_from=None,
            valid_to=None,
            reason="  ",
        )
    assert "reason" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        authz.assign_role(
            db,
            actor,
            user_id=admins["staff"].id,
            role_id=role_id,
            valid_from=None,
            valid_to=clock.now() - timedelta(days=1),
            reason=REASON,
        )
    assert "valid_to" in exc.value.field_errors


def test_overlapping_assignments_of_the_same_role_are_refused(
    people: People, db: Session, admins: dict
) -> None:
    actor = people.actor(admins["org_admin"])
    role_id = people.role("auditor").id
    authz.assign_role(
        db,
        actor,
        user_id=admins["staff"].id,
        role_id=role_id,
        valid_from=None,
        valid_to=None,
        reason=REASON,
    )
    with pytest.raises(Conflict):
        authz.assign_role(
            db,
            actor,
            user_id=admins["staff"].id,
            role_id=role_id,
            valid_from=clock.now() + timedelta(days=3),
            valid_to=None,
            reason=REASON,
        )


def test_acting_roles_start_and_end_on_their_own(people: People, db: Session, admins: dict) -> None:
    now = clock.now()
    authz.assign_role(
        db,
        people.actor(admins["org_admin"]),
        user_id=admins["staff"].id,
        role_id=people.role("auditor").id,
        valid_from=now + timedelta(days=2),
        valid_to=now + timedelta(days=9),
        reason=REASON,
    )
    assert not engine.check(db, admins["staff"].id, "audit.log.read")
    access = authz.user_access(db, people.actor(admins["org_admin"]), admins["staff"].id)
    assert [a.status for a in access.assignments] == ["upcoming"]


def test_the_last_super_admin_cannot_lose_the_role(
    people: People, db: Session, admins: dict
) -> None:
    second = people.add("Bola Ige")
    people.give(second, "super_administrator")
    admins["root"].status = "suspended"  # only active super admins count
    db.flush()
    assignment = next(
        a
        for a in authz.user_access(db, people.actor(admins["root"]), second.id).assignments
        if a.role_name == "Super Administrator"
    )
    with pytest.raises(Conflict, match="last active Super Administrator"):
        authz.end_assignment(db, people.actor(admins["root"]), assignment.id, REASON)
    with pytest.raises(PermissionDenied, match="your own roles"):
        authz.end_assignment(db, people.actor(second), assignment.id, REASON)
    assert authz.is_super_admin(db, second.id)


def test_the_last_super_admin_cannot_be_suspended(
    people: People, db: Session, admins: dict
) -> None:
    second = people.add("Bola Ige")
    people.give(second, "super_administrator")
    identity.suspend_user(db, people.actor(second), admins["root"].id)
    with pytest.raises(Conflict, match="last active Super Administrator"):
        identity.suspend_user(db, people.actor(admins["org_admin"]), second.id)


def test_users_list_shows_current_roles(people: People, db: Session, admins: dict) -> None:
    names = authz.role_names(db, [admins["root"].id, admins["staff"].id])
    assert names == {admins["root"].id: ["Super Administrator"]}


# ------------------------------------------------------------------ roles


def test_protected_roles_cannot_be_edited(people: People, db: Session, admins: dict) -> None:
    with pytest.raises(Conflict, match="protected"):
        authz.update_role(
            db,
            people.actor(admins["root"]),
            people.role("super_administrator").id,
            name="Root",
            description="",
            requires_mfa=True,
            grants={},
        )


def test_nobody_can_grant_a_role_permissions_they_lack(
    people: People, db: Session, admins: dict
) -> None:
    actor = people.actor(admins["org_admin"])
    role_id = authz.create_role(db, actor, name="Helpdesk", description="")
    with pytest.raises(PermissionDenied, match="mail"):
        authz.update_role(
            db,
            actor,
            role_id,
            name="Helpdesk",
            description="",
            requires_mfa=False,
            grants={"system.mail.manage": Scope.ORGANIZATION},
        )
    authz.update_role(
        db,
        actor,
        role_id,
        name="Helpdesk",
        description="",
        requires_mfa=False,
        grants={"identity.user.read": Scope.ORGANIZATION},
    )
    assert authz.get_role(db, role_id).grants == {"identity.user.read": Scope.ORGANIZATION}


def test_grants_must_use_a_scope_the_permission_allows(
    people: People, db: Session, admins: dict
) -> None:
    actor = people.actor(admins["root"])
    role_id = authz.create_role(db, actor, name="Odd", description="")
    with pytest.raises(ValidationFailed) as exc:
        authz.update_role(
            db,
            actor,
            role_id,
            name="Odd",
            description="",
            requires_mfa=False,
            grants={"system.mail.manage": Scope.SELF},
        )
    assert "system.mail.manage" in exc.value.field_errors


def test_role_names_are_unique_regardless_of_case(
    people: People, db: Session, admins: dict
) -> None:
    with pytest.raises(ValidationFailed):
        authz.create_role(db, people.actor(admins["root"]), name="auditor", description="")


def test_copying_a_role_copies_its_grants(people: People, db: Session, admins: dict) -> None:
    actor = people.actor(admins["org_admin"])
    copy_id = authz.create_role(
        db, actor, name="Junior auditor", description="", copy_from=people.role("auditor").id
    )
    assert (
        authz.get_role(db, copy_id).grants == authz.get_role(db, people.role("auditor").id).grants
    )


def test_built_in_and_used_roles_cannot_be_deleted(
    people: People, db: Session, admins: dict
) -> None:
    actor = people.actor(admins["root"])
    with pytest.raises(Conflict):
        authz.delete_role(db, actor, people.role("auditor").id)
    role_id = authz.create_role(db, actor, name="Temp", description="")
    authz.assign_role(
        db,
        actor,
        user_id=admins["staff"].id,
        role_id=role_id,
        valid_from=None,
        valid_to=None,
        reason=REASON,
    )
    with pytest.raises(Conflict, match="kept for the record"):
        authz.delete_role(db, actor, role_id)
    unused = authz.create_role(db, actor, name="Unused", description="")
    authz.delete_role(db, actor, unused)
    with pytest.raises(NotFound):
        authz.get_role(db, unused)


def test_catalogue_and_database_agree(db: Session) -> None:
    assert authz.catalogue_problems(db) == []


# ------------------------------------------------------------------ delegations


def _lend(
    db: Session,
    people: People,
    frm: object,
    to: object,
    keys: list[str],
    *,
    scope: Scope = Scope.ORGANIZATION,
    days: int = 7,
    **kwargs: object,
) -> list:
    return authz.create_delegation(
        db,
        people.actor(frm),
        to_user_id=to.id,  # type: ignore[attr-defined]
        permission_keys=keys,
        scope=scope,
        starts_at=None,
        ends_at=clock.now() + timedelta(days=days),
        reason=REASON,
        **kwargs,
    )


def test_people_can_lend_their_own_permissions(people: People, db: Session, admins: dict) -> None:
    _lend(db, people, admins["org_admin"], admins["staff"], ["identity.user.read"])
    assert engine.check(db, admins["staff"].id, "identity.user.read")
    mine = authz.list_delegations(db, people.actor(admins["staff"]))
    assert [d.permission_key for d in mine] == ["identity.user.read"]


def test_delegations_are_capped_at_the_configured_length(
    people: People, db: Session, admins: dict
) -> None:
    with pytest.raises(ValidationFailed) as exc:
        _lend(db, people, admins["org_admin"], admins["staff"], ["identity.user.read"], days=91)
    assert "90 days" in exc.value.field_errors["ends_at"]


def test_access_and_technical_permissions_cannot_be_delegated(
    people: People, db: Session, admins: dict
) -> None:
    for key in ("authz.role.manage", "system.mail.manage"):
        with pytest.raises(ValidationFailed, match="can't be delegated"):
            _lend(db, people, admins["root"], admins["staff"], [key])


def test_you_cannot_lend_what_you_do_not_hold(people: People, db: Session, admins: dict) -> None:
    other = people.add("Bola Ige")
    with pytest.raises(PermissionDenied):
        _lend(db, people, admins["staff"], other, ["identity.user.read"])


def test_delegating_on_someone_elses_behalf_needs_the_manage_permission(
    people: People, db: Session, admins: dict
) -> None:
    auditor = people.add("Kemi Ade")
    people.give(auditor, "auditor")
    deputy = people.add("Bola Ige")
    _lend(db, people, admins["org_admin"], deputy, ["audit.log.read"], from_user_id=auditor.id)
    assert engine.check(db, deputy.id, "audit.log.read")
    with pytest.raises(PermissionDenied):
        _lend(
            db,
            people,
            admins["staff"],
            deputy,
            ["identity.user.read"],
            from_user_id=admins["org_admin"].id,
        )
    with pytest.raises(PermissionDenied, match="to yourself"):
        _lend(
            db,
            people,
            admins["org_admin"],
            admins["org_admin"],
            ["audit.log.read"],
            from_user_id=auditor.id,
        )


def test_delegations_can_only_go_to_active_colleagues(
    people: People, db: Session, admins: dict
) -> None:
    gone = people.add("Bola Ige", status="suspended")
    with pytest.raises(ValidationFailed):
        _lend(db, people, admins["org_admin"], gone, ["identity.user.read"])


def test_revoking_is_for_the_people_involved_or_managers(
    people: People, db: Session, admins: dict
) -> None:
    [delegation_id] = _lend(
        db, people, admins["org_admin"], admins["staff"], ["identity.user.read"]
    )
    stranger = people.add("Bola Ige")
    with pytest.raises(NotFound):
        authz.revoke_delegation(db, people.actor(stranger), delegation_id)
    authz.revoke_delegation(db, people.actor(admins["staff"]), delegation_id)
    assert not engine.check(db, admins["staff"].id, "identity.user.read")


def test_suspending_someone_revokes_their_delegations(
    people: People, db: Session, admins: dict
) -> None:
    _lend(db, people, admins["org_admin"], admins["staff"], ["identity.user.read"])
    identity.suspend_user(db, people.actor(admins["root"]), admins["org_admin"].id)
    assert not engine.check(db, admins["staff"].id, "identity.user.read")
