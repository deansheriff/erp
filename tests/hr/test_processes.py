"""HR processes (ADR 0017): change requests, invitations, onboarding and offboarding
checklists, exits with automatic access revocation, and employee documents."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.authz import engine
from app.authz.fields import FieldClass
from app.authz.models import RoleAssignment, RoleGrant
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.models import ApiKey, User, UserSession
from app.mail.models import MailSettings
from app.modules.hr import checklists, documents, exits, records
from app.modules.hr import service as people_service
from app.modules.hr.models import (
    Checklist,
    ChecklistTask,
    DocumentCategory,
    Employee,
)
from app.modules.hr.schemas import PersonInput
from app.org.models import PositionAssignment
from tests.files.conftest import EicarScanner, png
from tests.hr.conftest import Hr, add_templates

pytestmark = pytest.mark.db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


# ------------------------------------------------------------------ change requests


def _request_bank(hr: Hr, who: str = "Efe") -> object:
    return records.request_change(
        hr.db,
        hr.actor(who),
        FieldClass.BANKING,
        {"bank_name": "Access Bank", "account_number": "0987654321", "account_name": "", "bvn": ""},
    )


def test_a_bank_change_waits_for_hr_then_applies(hr: Hr) -> None:
    request_id = _request_bank(hr)
    row = hr.db.get(Employee, hr.employee("Efe"))
    assert row is not None and row.account_number_encrypted is None  # not applied yet
    inbox = records.list_requests(hr.db, hr.actor("Dayo"))
    assert [r.employee_name for r in inbox] == ["Efe Test"]
    assert inbox[0].can_decide
    records.decide(hr.db, hr.actor("Dayo"), request_id, approve=True)  # type: ignore[arg-type]
    record = records.get_record(hr.db, hr.actor("Efe"), hr.employee("Efe"))
    bank = record.section("banking")
    assert bank is not None
    assert {f.field.name: f.display for f in bank.fields}["account_number"] == "0987654321"


def test_nobody_decides_their_own_request(hr: Hr) -> None:
    request_id = records.request_change(
        hr.db, hr.actor("Dayo"), FieldClass.STATUTORY, {"nin": "12345678901"}
    )
    with pytest.raises(PermissionDenied):
        records.decide(hr.db, hr.actor("Dayo"), request_id, approve=True)
    records.decide(hr.db, hr.actor("Chi"), request_id, approve=True)


def test_rejection_needs_a_note_and_colleagues_cannot_decide(hr: Hr) -> None:
    request_id = _request_bank(hr)
    with pytest.raises(ValidationFailed):
        records.decide(hr.db, hr.actor("Dayo"), request_id, approve=False)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        records.decide(hr.db, hr.actor("Femi"), request_id, approve=True)  # type: ignore[arg-type]
    records.decide(hr.db, hr.actor("Dayo"), request_id, approve=False, note="Wrong BVN")  # type: ignore[arg-type]
    assert records.my_requests(hr.db, hr.actor("Efe"))[0].status == "rejected"


def test_one_open_request_per_class_and_cancelling(hr: Hr) -> None:
    request_id = _request_bank(hr)
    with pytest.raises(Conflict):
        _request_bank(hr)
    records.cancel_request(hr.db, hr.actor("Efe"), request_id)  # type: ignore[arg-type]
    _request_bank(hr)
    with pytest.raises(NotFound):
        records.cancel_request(hr.db, hr.actor("Femi"), request_id)  # type: ignore[arg-type]


def test_requests_must_change_something_valid(hr: Hr) -> None:
    with pytest.raises(ValidationFailed):
        records.request_change(hr.db, hr.actor("Efe"), FieldClass.BANKING, {"bvn": "123"})
    with pytest.raises(ValidationFailed):
        records.request_change(hr.db, hr.actor("Efe"), FieldClass.BANKING, {})
    with pytest.raises(NotFound):
        records.request_change(hr.db, hr.actor("Efe"), FieldClass.PERSONAL, {"phone": "0801"})


# ------------------------------------------------------------------ invitations


def _mail_ready(hr: Hr) -> None:
    hr.db.add(
        MailSettings(
            host="localhost",
            port=1025,
            security="none",
            from_name="HR",
            from_address="hr@adeyemiholdings.ng",
        )
    )
    hr.db.flush()


def test_inviting_links_the_account_and_gives_the_employee_role(hr: Hr) -> None:
    _mail_ready(hr)
    new = people_service.add_person(
        hr.db,
        hr.actor("Dayo"),
        PersonInput(first_name="Ngozi", last_name="Adeyemi", work_email="ngozi@adeyemiholdings.ng"),
    )
    user_id = exits.invite(hr.db, hr.actor("Dayo"), new)
    assert hr.db.get(Employee, new).user_id == user_id  # type: ignore[union-attr]
    assert engine.check(hr.db, user_id, "hr.profile.request_change")
    with pytest.raises(Conflict):
        exits.invite(hr.db, hr.actor("Dayo"), new)


def test_a_widened_employee_role_is_not_given_automatically(hr: Hr) -> None:
    _mail_ready(hr)
    role = hr.people.role("employee")
    grant = hr.db.get(RoleGrant, (role.id, "hr.employee.banking.read"))
    assert grant is not None
    grant.scope = "organization"
    hr.db.flush()
    new = people_service.add_person(
        hr.db,
        hr.actor("Dayo"),
        PersonInput(first_name="Tunde", last_name="B", work_email="tunde@adeyemiholdings.ng"),
    )
    with pytest.raises(Conflict, match="Employee role"):
        exits.invite(hr.db, hr.actor("Dayo"), new)


# ------------------------------------------------------------------ checklists


def test_the_most_specific_template_wins(hr: Hr) -> None:
    add_templates(hr.db, hr)
    fin = checklists.save_template(
        hr.db,
        hr.actor("Chi"),
        None,
        kind="onboarding",
        name="Finance onboarding",
        department_id=hr.org.departments["FIN"],
        position_id=None,
        is_active=True,
        tasks=[("Finance systems access", "it", "0")],
    )
    accountant = checklists.save_template(
        hr.db,
        hr.actor("Chi"),
        None,
        kind="onboarding",
        name="Accountant onboarding",
        department_id=None,
        position_id=hr.org.positions["Accountant"],
        is_active=True,
        tasks=[("ICAN certificate", "hr", "0")],
    )
    pick = checklists.choose_template
    assert pick(hr.db, "onboarding", hr.employee("Efe")).id == accountant  # type: ignore[union-attr]
    assert pick(hr.db, "onboarding", hr.employee("Bola")).id == fin  # type: ignore[union-attr]
    assert pick(hr.db, "onboarding", hr.employee("Chi")).name == "Default onboarding"  # type: ignore[union-attr]


def test_tasks_are_completed_by_their_assignees(hr: Hr) -> None:
    add_templates(hr.db, hr)
    checklist_id = checklists.start_onboarding(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    tasks = {
        t.assignee: t.id
        for t in hr.db.scalars(
            select(ChecklistTask).where(ChecklistTask.checklist_id == checklist_id)
        )
    }
    checklist = hr.db.get(Checklist, checklist_id)
    assert checklist is not None and checklist.manager_employee_id == hr.employee("Gbenga")
    with pytest.raises(PermissionDenied):
        checklists.set_done(hr.db, hr.actor("Femi"), tasks["employee"], done=True)
    with pytest.raises(PermissionDenied):
        checklists.set_done(hr.db, hr.actor("Efe"), tasks["it"], done=True)
    checklists.set_done(hr.db, hr.actor("Efe"), tasks["employee"], done=True)
    checklists.set_done(hr.db, hr.actor("Hauwa"), tasks["it"], done=True)  # IT Support
    checklists.set_done(hr.db, hr.actor("Gbenga"), tasks["manager"], done=True)  # acting manager
    assert hr.db.get(Checklist, checklist_id).completed_at is not None  # type: ignore[union-attr]
    mine = checklists.my_open_tasks(hr.db, hr.actor("Efe"))
    assert mine == []


def test_onboarding_needs_a_template_and_runs_once(hr: Hr) -> None:
    with pytest.raises(Conflict, match="template"):
        checklists.start_onboarding(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    add_templates(hr.db, hr)
    checklists.start_onboarding(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    with pytest.raises(Conflict):
        checklists.start_onboarding(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    with pytest.raises(PermissionDenied):
        checklists.start_onboarding(hr.db, hr.actor("Efe"), hr.employee("Femi"))


# ------------------------------------------------------------------ exits


def _record_exit(hr: Hr, who: str, last_day_offset: int, by: str = "Dayo") -> object:
    today = clock.today()
    return exits.record_exit(
        hr.db,
        hr.actor(by),
        hr.employee(who),
        kind="resignation",
        reason="Relocating to Abuja",
        notice_date=today - timedelta(days=30),
        last_working_day=today + timedelta(days=last_day_offset),
        final_pay_due=True,
    )


def test_access_ends_after_the_last_working_day(hr: Hr) -> None:
    add_templates(hr.db, hr)
    user_id = hr.org.users["Efe"]
    hr.db.add(
        UserSession(
            user_id=user_id,
            token_hash="x" * 64,
            csrf_token="c" * 64,
            expires_at=clock.now() + timedelta(days=1),
        )
    )
    hr.db.flush()
    _record_exit(hr, "Efe", last_day_offset=0)
    assert exits.revoke_due(hr.db) == 0  # still their last day
    assert hr.db.get(User, user_id).status == "active"  # type: ignore[union-attr]
    offboarding = hr.db.scalars(select(Checklist).where(Checklist.kind == "offboarding")).one()
    assert offboarding.anchor_date == clock.today()

    tomorrow = clock.now() + timedelta(days=1)
    assert exits.revoke_due(hr.db, tomorrow) == 1
    hr.db.flush()
    hr.db.expire_all()
    assert hr.db.get(User, user_id).status == "deactivated"  # type: ignore[union-attr]
    assert hr.db.get(Employee, hr.employee("Efe")).status == "terminated"  # type: ignore[union-attr]
    assert all(
        s.revoked_at
        for s in hr.db.scalars(select(UserSession).where(UserSession.user_id == user_id))
    )
    assert not hr.db.scalars(
        select(RoleAssignment).where(
            RoleAssignment.user_id == user_id, RoleAssignment.ended_at.is_(None)
        )
    ).all()
    position = hr.db.scalars(
        select(PositionAssignment).where(PositionAssignment.employee_id == hr.employee("Efe"))
    ).one()
    assert position.end_date == clock.today()
    assert not engine.check(hr.db, user_id, "hr.employee.read")
    assert exits.revoke_due(hr.db, tomorrow) == 0  # done once


def test_an_exit_in_the_past_revokes_at_once(hr: Hr) -> None:
    hr.db.add(ApiKey(user_id=hr.org.users["Femi"], name="cli", key_hash="k" * 64, prefix="kx_test"))
    hr.db.flush()
    _record_exit(hr, "Femi", last_day_offset=-1)
    assert hr.db.get(Employee, hr.employee("Femi")).status == "terminated"  # type: ignore[union-attr]
    key = hr.db.scalars(select(ApiKey).where(ApiKey.user_id == hr.org.users["Femi"])).one()
    assert key.revoked_at is not None


def test_exits_can_be_cancelled_before_revocation(hr: Hr) -> None:
    add_templates(hr.db, hr)
    exit_id = _record_exit(hr, "Efe", last_day_offset=10)
    with pytest.raises(Conflict):
        _record_exit(hr, "Efe", last_day_offset=12)
    exits.cancel_exit(hr.db, hr.actor("Dayo"), exit_id)  # type: ignore[arg-type]
    assert (
        hr.db.scalars(select(Checklist).where(Checklist.kind == "offboarding")).one().cancelled_at
    )
    assert exits.revoke_due(hr.db, clock.now() + timedelta(days=30)) == 0
    _record_exit(hr, "Efe", last_day_offset=5)  # a new exit is allowed after cancelling


def test_exit_rules(hr: Hr) -> None:
    with pytest.raises(PermissionDenied):
        _record_exit(hr, "Dayo", last_day_offset=3, by="Dayo")  # not your own
    with pytest.raises(PermissionDenied):
        _record_exit(hr, "Efe", last_day_offset=3, by="Bola")  # no hr.exit.manage
    with pytest.raises(ValidationFailed):
        exits.record_exit(
            hr.db,
            hr.actor("Dayo"),
            hr.employee("Efe"),
            kind="fired",
            reason="x",
            notice_date=clock.today(),
            last_working_day=clock.today() - timedelta(days=1),
            final_pay_due=False,
        )


def test_the_last_super_admin_cannot_be_given_an_exit(hr: Hr) -> None:
    user = hr.db.get(User, hr.org.users["Ada"])
    assert user is not None
    hr.people.give(user, "super_administrator")
    with pytest.raises(Conflict, match="Super Administrator"):
        _record_exit(hr, "Ada", last_day_offset=3)


# ------------------------------------------------------------------ documents


def _category(hr: Hr, name: str, field_class: str) -> DocumentCategory:
    row = DocumentCategory(name=name, field_class=field_class)
    hr.db.add(row)
    hr.db.flush()
    return row


def test_document_visibility_follows_its_class(hr: Hr, scanner: EicarScanner) -> None:
    contract = _category(hr, "Contract (test)", "compensation")
    certificate = _category(hr, "Certificate (test)", "personal")
    with pytest.raises(PermissionDenied):  # HR Officer can't file pay documents
        documents.add_document(
            hr.db,
            hr.actor("Dayo"),
            hr.employee("Efe"),
            category_id=contract.id,
            title="Contract",
            filename="contract.pdf",
            declared_mime="application/pdf",
            data=PDF,
        )
    documents.add_document(
        hr.db,
        hr.actor("Chi"),
        hr.employee("Efe"),
        category_id=contract.id,
        title="Contract",
        filename="contract.pdf",
        declared_mime="application/pdf",
        data=PDF,
    )
    documents.add_document(
        hr.db,
        hr.actor("Dayo"),
        hr.employee("Efe"),
        category_id=certificate.id,
        title="BSc",
        filename="bsc.png",
        declared_mime="image/png",
        data=png(),
    )

    def titles(who: str) -> list[str]:
        efe = hr.employee("Efe")
        return sorted(d.title for d in documents.list_documents(hr.db, hr.actor(who), efe))

    assert titles("Chi") == ["BSc", "Contract"]
    assert titles("Efe") == ["BSc", "Contract"]  # their own
    assert titles("Dayo") == ["BSc"]  # no compensation class
    assert titles("Bola") == ["BSc"]  # line manager: team's personal class
    assert titles("Femi") == []  # a colleague sees the directory entry, no documents


def test_documents_are_replaced_and_deleted(hr: Hr, scanner: EicarScanner) -> None:
    certificate = _category(hr, "Certificate (test)", "personal")
    doc = documents.add_document(
        hr.db,
        hr.actor("Dayo"),
        hr.employee("Efe"),
        category_id=certificate.id,
        title="BSc",
        filename="bsc.png",
        declared_mime="image/png",
        data=png(),
    )
    documents.replace_document(
        hr.db, hr.actor("Dayo"), doc, filename="bsc2.png", declared_mime="image/png", data=png(3, 3)
    )
    listed = documents.list_documents(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    assert listed[0].file is not None and listed[0].file.version == 2
    with pytest.raises(PermissionDenied):
        documents.delete_document(hr.db, hr.actor("Bola"), doc)
    documents.delete_document(hr.db, hr.actor("Dayo"), doc)
    assert documents.list_documents(hr.db, hr.actor("Dayo"), hr.employee("Efe")) == []
