"""Field-level visibility on the HR record (brief 4.1, ADR 0017): who reads and changes
which classes, encryption at rest, audit masking and employee numbers."""

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.audit.service import MASK
from app.authz.fields import FieldClass
from app.core.errors import PermissionDenied, ValidationFailed
from app.core.secrets import decrypt
from app.modules.hr import records
from app.modules.hr import service as people_service
from app.modules.hr.models import Employee
from app.modules.hr.schemas import PersonInput
from tests.hr.conftest import Hr

pytestmark = pytest.mark.db

P, S, B = FieldClass.PERSONAL, FieldClass.STATUTORY, FieldClass.BANKING
DETAILS = {
    "phone": "0803 123 4567",
    "hire_date": "2025-02-03",
    "nin": "12345678901",
    "bank_name": "GTBank",
    "account_number": "0123456789",
    "bvn": "22233344455",
}


def _fill(hr: Hr, who: str = "Efe") -> None:
    records.update_fields(hr.db, hr.actor("Chi"), hr.employee(who), DETAILS)


def _classes(hr: Hr, viewer: str, subject: str) -> set[FieldClass]:
    record = records.get_record(hr.db, hr.actor(viewer), hr.employee(subject))
    return {s.field_class for s in record.sections}


@pytest.mark.parametrize(
    ("viewer", "subject", "expected"),
    [
        ("Efe", "Efe", {P, S, B}),  # everything about themselves
        ("Efe", "Femi", set()),  # a colleague: directory only
        ("Bola", "Efe", {P}),  # line manager: team's personal details
        ("Bola", "Dayo", set()),  # not in Bola's team
        ("Dayo", "Efe", {P, S, B}),  # HR Officer
        ("Chi", "Efe", {P, S, B}),  # HR Manager
        ("Hauwa", "Efe", set()),  # IT Support
    ],
)
def test_sections_follow_the_viewers_classes(
    hr: Hr, viewer: str, subject: str, expected: set[FieldClass]
) -> None:
    assert _classes(hr, viewer, subject) == expected


def test_hidden_values_never_leave_the_service(hr: Hr) -> None:
    _fill(hr)
    record = records.get_record(hr.db, hr.actor("Femi"), hr.employee("Efe"))
    rendered = repr(record)
    for value in DETAILS.values():
        assert value not in rendered
    assert record.full_name == "Efe Test"


def test_hr_officer_does_not_see_pay_or_sensitive(hr: Hr) -> None:
    access = records.access(hr.db, hr.actor("Dayo"), hr.employee("Efe"))
    assert FieldClass.COMPENSATION not in access.read
    assert FieldClass.SENSITIVE not in access.read
    manager = records.access(hr.db, hr.actor("Chi"), hr.employee("Efe"))
    assert {FieldClass.COMPENSATION, FieldClass.SENSITIVE} <= manager.read


def test_identifiers_are_encrypted_at_rest(hr: Hr) -> None:
    _fill(hr)
    row = hr.db.get(Employee, hr.employee("Efe"))
    assert row is not None and row.nin_encrypted and row.account_number_encrypted
    assert "12345678901" not in row.nin_encrypted
    assert decrypt(row.nin_encrypted) == "12345678901"
    assert row.bank_name == "GTBank"  # names stay plain


def test_audit_masks_statutory_and_banking_values(hr: Hr) -> None:
    _fill(hr)
    event = hr.db.scalars(
        select(AuditLog).where(AuditLog.action == "hr.employee.update_details")
    ).one()
    text = str(event.changes)
    assert "12345678901" not in text and "0123456789" not in text
    assert "0803 123 4567" in text  # personal values are recorded
    assert event.changes["nin"] == {"from": MASK, "to": MASK}


def test_employees_change_only_their_self_service_fields(hr: Hr) -> None:
    efe = hr.actor("Efe")
    records.update_fields(hr.db, efe, hr.employee("Efe"), {"phone": "0802 000 0000"})
    with pytest.raises(PermissionDenied):
        records.update_fields(hr.db, efe, hr.employee("Efe"), {"hire_date": "2020-01-01"})
    with pytest.raises(PermissionDenied):
        records.update_fields(hr.db, efe, hr.employee("Efe"), {"nin": "12345678901"})
    with pytest.raises(PermissionDenied):  # a colleague: directory read only
        records.update_fields(hr.db, efe, hr.employee("Femi"), {"phone": "0802 000 0000"})


def test_hr_staff_cannot_change_their_own_hr_managed_fields(hr: Hr) -> None:
    dayo = hr.actor("Dayo")
    records.update_fields(hr.db, dayo, hr.employee("Dayo"), {"phone": "0805 555 5555"})
    with pytest.raises(PermissionDenied):
        records.update_fields(hr.db, dayo, hr.employee("Dayo"), {"hire_date": "2015-01-01"})
    with pytest.raises(PermissionDenied):
        records.update_fields(hr.db, dayo, hr.employee("Dayo"), {"account_number": "0123456789"})


def test_line_managers_read_but_do_not_change(hr: Hr) -> None:
    with pytest.raises(PermissionDenied):
        records.update_fields(hr.db, hr.actor("Bola"), hr.employee("Efe"), {"phone": "0801"})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nin", "1234", "The NIN is 11 digits."),
        ("bvn", "abc", "The BVN is 11 digits."),
        ("account_number", "12345", "The account number (NUBAN) is 10 digits."),
        ("rsa_pin", "12345", "The RSA PIN is PEN followed by 12 digits."),
        ("gender", "other", "Choose one of the options."),
        ("hire_date", "yesterday", "Enter a date."),
    ],
)
def test_values_are_checked(hr: Hr, field: str, value: str, message: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        records.update_fields(hr.db, hr.actor("Chi"), hr.employee("Efe"), {field: value})
    assert exc.value.field_errors == {field: message}


def test_nysc_and_probation_dates_must_make_sense(hr: Hr) -> None:
    with pytest.raises(ValidationFailed) as exc:
        records.update_fields(
            hr.db,
            hr.actor("Chi"),
            hr.employee("Efe"),
            {
                "nysc_start": "2026-03-01",
                "nysc_end": "2026-01-01",
                "hire_date": "2026-01-01",
                "probation_end": "2025-12-01",
            },
        )
    assert set(exc.value.field_errors) == {"nysc_end", "probation_end"}


def test_employee_numbers_are_gap_free(hr: Hr) -> None:
    chi = hr.actor("Chi")
    first = people_service.add_person(hr.db, chi, PersonInput(first_name="Ngozi", last_name="A"))
    savepoint = hr.db.begin_nested()
    people_service.add_person(hr.db, chi, PersonInput(first_name="Rolled", last_name="Back"))
    savepoint.rollback()
    second = people_service.add_person(hr.db, chi, PersonInput(first_name="Kelechi", last_name="B"))
    numbers = [hr.db.get(Employee, i).employee_number for i in (first, second)]  # type: ignore[union-attr]
    assert numbers == ["EMP-00001", "EMP-00002"]


def test_number_format_is_configurable(hr: Hr) -> None:
    records.configure_numbers(hr.db, hr.actor("Chi"), prefix="ah/", padding="4")
    new = people_service.add_person(
        hr.db, hr.actor("Chi"), PersonInput(first_name="Halima", last_name="S")
    )
    assert hr.db.get(Employee, new).employee_number == "AH/0001"  # type: ignore[union-attr]
    with pytest.raises(ValidationFailed):
        records.configure_numbers(hr.db, hr.actor("Chi"), prefix="EMP-", padding="20")
    with pytest.raises(PermissionDenied):
        records.configure_numbers(hr.db, hr.actor("Dayo"), prefix="X", padding="3")


def test_status_terminated_is_only_set_by_an_exit(hr: Hr) -> None:
    with pytest.raises(ValueError, match="Choose one of the options"):
        PersonInput(first_name="A", last_name="B", status="terminated")
