"""Leave (ADR 0018): day counting, entitlements and balances, requests and the approval
chain, certificates, cancelling, adjustments and carry-over."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.modules.leave import balances, days, service, settings
from app.modules.leave.models import EntitlementRule, LeaveAdjustment, LeaveRequest, PublicHoliday
from tests.files.conftest import EicarScanner
from tests.hr.conftest import Hr
from tests.leave.conftest import leave_type

pytestmark = pytest.mark.db

YEAR = clock.today().year + 1  # a whole future year, so "today" never cuts it short
MON = date(YEAR, 3, 2) - timedelta(days=date(YEAR, 3, 2).weekday())  # a Monday in March


def _ask(hr: Hr, who: str, code: str, start: date, end: date, **extra: object) -> object:
    return service.request_leave(
        hr.db,
        hr.actor(who),
        hr.employee(who),
        leave_type_id=leave_type(hr.db, code).id,
        start=start,
        end=end,
        **extra,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ counting days


def test_working_days_skip_weekends_and_holidays(staff: Hr) -> None:
    db = staff.db
    friday = MON + timedelta(days=4)
    assert days.count(db, MON, friday, counting="working") == 5
    assert days.count(db, MON, MON + timedelta(days=7), counting="working") == 6
    db.add(PublicHoliday(day=MON + timedelta(days=2), name="Test holiday"))
    db.flush()
    assert days.count(db, MON, friday, counting="working") == 4
    assert days.count(db, MON, friday, counting="working", start_half=True, end_half=True) == 3
    assert days.count(db, MON, MON, counting="working", start_half=True) == Decimal("0.5")
    assert days.count(db, MON, MON + timedelta(days=83), counting="calendar") == 84


@pytest.mark.parametrize(
    ("start_offset", "end_offset", "field"),
    [(3, 1, "end_date"), (5, 6, "end_date")],  # backwards; a weekend only
)
def test_impossible_dates_are_refused(
    staff: Hr, start_offset: int, end_offset: int, field: str
) -> None:
    with pytest.raises(ValidationFailed) as exc:
        days.count(
            staff.db,
            MON + timedelta(days=start_offset),
            MON + timedelta(days=end_offset),
            counting="working",
        )
    assert field in exc.value.field_errors


def test_requests_do_not_cross_the_new_year(staff: Hr) -> None:
    with pytest.raises(ValidationFailed, match=""):
        days.count(staff.db, date(YEAR, 12, 30), date(YEAR + 1, 1, 2), counting="working")


# ------------------------------------------------------------------ entitlements


def _entitlement(code: str, hired: date | None, year: int, db: object) -> Decimal:
    t = leave_type(db, code)  # type: ignore[arg-type]
    rules = list(db.scalars(select(EntitlementRule).where(EntitlementRule.leave_type_id == t.id)))  # type: ignore[attr-defined]
    return balances.entitlement(
        t, rules, hire_date=hired, grade_id=None, year=year, today=date(year, 12, 31)
    )


def test_annual_leave_starts_after_twelve_months_and_is_pro_rated(staff: Hr) -> None:
    db = staff.db
    assert _entitlement("annual", date(2020, 1, 6), YEAR, db) == 6
    assert _entitlement("annual", date(YEAR, 2, 1), YEAR, db) == 0  # not yet 12 months
    # Eligible on 1 July of the year: half the year left, 3 days.
    assert _entitlement("annual", date(YEAR - 1, 7, 1), YEAR, db) == 3
    assert _entitlement("annual", None, YEAR, db) == 0  # no hire date, no service-based days
    assert _entitlement("sick", date(YEAR, 7, 1), YEAR, db) == 6  # joiners: pro-rated
    assert _entitlement("paternity", date(2020, 1, 6), YEAR, db) == 0  # until HR sets it


def test_grade_rules_win_over_any_grade(staff: Hr) -> None:
    from app.org.models import Grade

    db = staff.db
    grade = Grade(code="GL-12", name="Manager", rank=12)
    db.add(grade)
    db.flush()
    t = leave_type(db, "annual")
    rules = list(db.scalars(select(EntitlementRule).where(EntitlementRule.leave_type_id == t.id)))
    rules.append(
        EntitlementRule(
            leave_type_id=t.id, grade_id=grade.id, min_service_months=0, days=Decimal(20)
        )
    )
    hired = date(2020, 1, 6)
    assert (
        balances.entitlement(
            t, rules, hire_date=hired, grade_id=grade.id, year=YEAR, today=date(YEAR, 1, 1)
        )
        == 20
    )
    assert (
        balances.entitlement(
            t, rules, hire_date=hired, grade_id=None, year=YEAR, today=date(YEAR, 1, 1)
        )
        == 6
    )


# ------------------------------------------------------------------ requests and approval


def test_request_manager_approval_and_balance(staff: Hr) -> None:
    db = staff.db
    request_id = _ask(staff, "Efe", "annual", MON, MON + timedelta(days=2))
    request = db.get(LeaveRequest, request_id)
    assert request is not None and request.step == "manager"
    assert request.manager_employee_id == staff.employee("Gbenga")  # acting manager
    b = balances.balance(db, staff.employee("Efe"), leave_type(db, "annual"), YEAR)
    assert (b.entitlement, b.pending, b.available) == (6, 3, 3)
    inbox = service.inbox(db, staff.actor("Gbenga"))
    assert [r.employee_name for r in inbox] == ["Efe Test"]
    service.decide(db, staff.actor("Gbenga"), request_id, approve=True)  # type: ignore[arg-type]
    assert request.status == "approved"
    b = balances.balance(db, staff.employee("Efe"), leave_type(db, "annual"), YEAR)
    assert (b.taken, b.pending, b.available) == (3, 0, 3)


def test_requests_over_the_balance_or_overlapping_are_refused(staff: Hr) -> None:
    with pytest.raises(ValidationFailed, match="") as exc:
        _ask(staff, "Efe", "annual", MON, MON + timedelta(days=13))  # 10 working days
    assert "6 are available" in exc.value.field_errors["end_date"]
    _ask(staff, "Efe", "annual", MON, MON + timedelta(days=1))
    with pytest.raises(ValidationFailed):
        _ask(staff, "Efe", "sick", MON + timedelta(days=1), MON + timedelta(days=1))


def test_nobody_approves_their_own_leave(staff: Hr) -> None:
    request_id = _ask(staff, "Chi", "annual", MON, MON)  # Chi: HR Manager, reports to Ada
    with pytest.raises(PermissionDenied):
        service.decide(staff.db, staff.actor("Chi"), request_id, approve=True)  # type: ignore[arg-type]
    service.decide(staff.db, staff.actor("Ada"), request_id, approve=True)  # type: ignore[arg-type]


def test_colleagues_cannot_see_or_decide(staff: Hr) -> None:
    request_id = _ask(staff, "Efe", "annual", MON, MON)
    with pytest.raises(NotFound):
        service.decide(staff.db, staff.actor("Femi"), request_id, approve=True)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        service.get_request(staff.db, staff.actor("Femi"), request_id)  # type: ignore[arg-type]


def test_hr_approval_follows_the_manager_when_the_type_needs_it(staff: Hr) -> None:
    db = staff.db
    request_id = _ask(staff, "Efe", "unpaid", MON, MON + timedelta(days=4))
    service.decide(db, staff.actor("Gbenga"), request_id, approve=True)  # type: ignore[arg-type]
    request = db.get(LeaveRequest, request_id)
    assert request is not None and (request.status, request.step) == ("pending", "hr")
    assert not service.inbox(db, staff.actor("Gbenga"))
    assert [r.id for r in service.inbox(db, staff.actor("Dayo"))] == [request_id]
    service.decide(db, staff.actor("Dayo"), request_id, approve=True)  # type: ignore[arg-type]
    assert request.status == "approved"


def test_people_without_a_manager_go_to_hr(staff: Hr) -> None:
    request_id = _ask(staff, "Hauwa", "annual", MON, MON)  # no position, so no manager
    assert staff.db.get(LeaveRequest, request_id).step == "hr"  # type: ignore[union-attr]
    service.decide(staff.db, staff.actor("Dayo"), request_id, approve=True)  # type: ignore[arg-type]


def test_rejection_needs_a_note_and_restores_the_balance(staff: Hr) -> None:
    db = staff.db
    request_id = _ask(staff, "Efe", "annual", MON, MON + timedelta(days=1))
    with pytest.raises(ValidationFailed):
        service.decide(db, staff.actor("Gbenga"), request_id, approve=False)  # type: ignore[arg-type]
    service.decide(db, staff.actor("Gbenga"), request_id, approve=False, note="Month-end close")  # type: ignore[arg-type]
    b = balances.balance(db, staff.employee("Efe"), leave_type(db, "annual"), YEAR)
    assert b.available == 6


def test_long_sick_leave_needs_a_certificate_before_approval(
    staff: Hr, scanner: EicarScanner
) -> None:
    db = staff.db
    request_id = _ask(staff, "Efe", "sick", MON, MON + timedelta(days=2))  # 3 days > 2
    with pytest.raises(Conflict, match="certificate"):
        service.decide(db, staff.actor("Gbenga"), request_id, approve=True)  # type: ignore[arg-type]
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    service.attach_certificate(
        db,
        staff.actor("Efe"),
        request_id,
        filename="note.pdf",
        declared_mime="application/pdf",
        data=pdf,  # type: ignore[arg-type]
    )
    view = service.get_request(db, staff.actor("Gbenga"), request_id)  # type: ignore[arg-type]
    assert view.certificate_file_id is not None
    service.decide(db, staff.actor("Gbenga"), request_id, approve=True)  # type: ignore[arg-type]
    short = _ask(staff, "Efe", "sick", MON + timedelta(days=7), MON + timedelta(days=8))
    service.decide(db, staff.actor("Gbenga"), short, approve=True)  # type: ignore[arg-type]


def test_cancelling(staff: Hr) -> None:
    db = staff.db
    request_id = _ask(staff, "Efe", "annual", MON, MON)
    service.decide(db, staff.actor("Gbenga"), request_id, approve=True)  # type: ignore[arg-type]
    service.cancel(db, staff.actor("Efe"), request_id)  # type: ignore[arg-type]  # not started yet
    assert (
        balances.balance(db, staff.employee("Efe"), leave_type(db, "annual"), YEAR).available == 6
    )
    with pytest.raises(Conflict):
        service.cancel(db, staff.actor("Efe"), request_id)  # type: ignore[arg-type]
    started = LeaveRequest(
        employee_id=staff.employee("Efe"),
        leave_type_id=leave_type(db, "annual").id,
        start_date=clock.today() - timedelta(days=1),
        end_date=clock.today(),
        days=Decimal(1),
        year=clock.today().year,
        status="approved",
    )
    db.add(started)
    db.flush()
    with pytest.raises(PermissionDenied):
        service.cancel(db, staff.actor("Efe"), started.id)
    service.cancel(db, staff.actor("Chi"), started.id)  # HR Manager holds leave.request.cancel


# ------------------------------------------------------------------ balances admin


def test_adjustments_are_audited_and_not_for_yourself(staff: Hr) -> None:
    db = staff.db
    annual = leave_type(db, "annual")
    settings.adjust(
        db,
        staff.actor("Chi"),
        staff.employee("Efe"),
        leave_type_id=annual.id,
        year=YEAR,
        days="+1.5",
        reason="Worked a public holiday",
    )
    assert balances.balance(db, staff.employee("Efe"), annual, YEAR).available == Decimal("7.5")
    with pytest.raises(PermissionDenied):
        settings.adjust(
            db,
            staff.actor("Chi"),
            staff.employee("Chi"),
            leave_type_id=annual.id,
            year=YEAR,
            days="5",
            reason="Bonus days",
        )
    with pytest.raises(PermissionDenied):
        settings.adjust(
            db,
            staff.actor("Dayo"),
            staff.employee("Efe"),
            leave_type_id=annual.id,
            year=YEAR,
            days="1",
            reason="Not allowed",
        )
    with pytest.raises(ValidationFailed):
        settings.adjust(
            db,
            staff.actor("Chi"),
            staff.employee("Efe"),
            leave_type_id=annual.id,
            year=YEAR,
            days="0.3",
            reason="Odd amount",
        )


def test_carry_over_is_capped_expires_and_runs_once(staff: Hr) -> None:
    db = staff.db
    annual = leave_type(db, "annual")
    annual.carry_over_max_days = Decimal(4)
    annual.carry_over_expiry_month = 3
    db.flush()
    assert settings.carry_over(db, YEAR) > 0
    assert settings.carry_over(db, YEAR) == 0  # already written
    row = db.scalars(
        select(LeaveAdjustment).where(
            LeaveAdjustment.employee_id == staff.employee("Efe"),
            LeaveAdjustment.kind == "carry_over",
        )
    ).one()
    assert row.days == 4 and row.expires_on == date(YEAR, 3, 31)
    b = balances.balance(db, staff.employee("Efe"), annual, YEAR)
    assert b.carried_over == 4  # not expired yet: YEAR is in the future


def test_settings_need_the_permission(staff: Hr) -> None:
    with pytest.raises(PermissionDenied):
        settings.add_holiday(staff.db, staff.actor("Dayo"), day=MON, name="Not allowed")
    settings.add_holiday(staff.db, staff.actor("Chi"), day=MON, name="Special day")
    with pytest.raises(ValidationFailed):
        settings.add_holiday(staff.db, staff.actor("Chi"), day=MON, name="Twice")


def test_hr_can_step_in_on_the_manager_step(staff: Hr) -> None:
    request_id = _ask(staff, "Efe", "annual", MON, MON)
    assert not service.inbox(staff.db, staff.actor("Dayo"))  # not in HR's queue
    service.decide(staff.db, staff.actor("Dayo"), request_id, approve=True)  # type: ignore[arg-type]
    assert staff.db.get(LeaveRequest, request_id).status == "approved"  # type: ignore[union-attr]
