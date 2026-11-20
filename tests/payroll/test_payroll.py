"""Payroll runs (ADR 0021): who is in a run, proration, overtime and deductions, rent
relief, year-to-date PAYE, maker-checker at each step, payslip PDFs and when employees
see them, and pay changes."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.modules.attendance.models import AttendanceRecord, OvertimeEntry
from app.modules.hr.models import Employee, EmployeeExit
from app.modules.payroll import reliefs, service, setup
from app.modules.payroll.models import Payslip
from tests.payroll.conftest import Payroll

pytestmark = pytest.mark.db

D = Decimal
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _slip(p: Payroll, run: object, who: str) -> Payslip:
    return p.hr.db.scalars(
        select(Payslip).where(Payslip.run_id == run, Payslip.employee_id == p.hr.employee(who))
    ).one()


# ------------------------------------------------------------------ calculating


def test_a_run_pays_everyone_in_the_group(payroll: Payroll) -> None:
    run = payroll.run(1)
    assert service.calculate(payroll.hr.db, payroll.hr.actor("Gbenga"), run) == 2
    efe = _slip(payroll, run, "Efe")
    # The standard month worked in test_calc: PAYE 39,560 on 317,000 taxable.
    assert (efe.gross, efe.paye, efe.net) == (D(350000), D("39560.00"), D("277440.00"))
    view = service.get_run(payroll.hr.db, payroll.hr.actor("Ada"), run)
    assert (view.people, view.gross) == (2, D(520000))
    assert view.employer_cost == D(520000) + view.pension_employer + view.nsitf
    with pytest.raises(Conflict, match="already"):
        payroll.run(1)


def test_joiners_and_leavers_are_prorated(payroll: Payroll) -> None:
    db = payroll.hr.db
    efe = db.get(Employee, payroll.hr.employee("Efe"))
    assert efe is not None
    efe.hire_date = date(2026, 1, 16)  # 16 of 31 days in January
    db.add(
        EmployeeExit(
            employee_id=payroll.hr.employee("Hauwa"),
            kind="resignation",
            reason="Moving abroad",
            notice_date=date(2026, 1, 2),
            last_working_day=date(2026, 1, 10),
        )
    )
    db.flush()
    run = payroll.run(1)
    service.calculate(db, payroll.hr.actor("Gbenga"), run)
    assert (_slip(payroll, run, "Efe").days_paid, _slip(payroll, run, "Efe").gross) == (
        16,
        D("180645.16"),  # 350,000 x 16/31, rounded per component
    )
    assert _slip(payroll, run, "Hauwa").days_paid == 10
    february = payroll.run(2)
    service.calculate(db, payroll.hr.actor("Gbenga"), february)
    assert [s.name for s in service.get_run(db, payroll.hr.actor("Ada"), february).payslips] == [
        "Efe Test"
    ]


def test_overtime_and_deductions_flow_in(payroll: Payroll) -> None:
    db = payroll.hr.db
    record = AttendanceRecord(
        employee_id=payroll.hr.employee("Efe"),
        work_date=date(2026, 1, 12),
        clock_in_at=datetime(2026, 1, 12, 7, 0, tzinfo=UTC),
        clock_out_at=datetime(2026, 1, 12, 19, 0, tzinfo=UTC),
    )
    db.add(record)
    db.flush()
    for status, minutes in (("approved", 120), ("pending", 600)):
        db.add(
            OvertimeEntry(
                record_id=record.id if status == "approved" else _second_record(payroll).id,
                employee_id=payroll.hr.employee("Efe"),
                work_date=date(2026, 1, 12 if status == "approved" else 13),
                minutes=minutes,
                status=status,
            )
        )
    setup.add_deduction(
        db,
        payroll.hr.actor("Gbenga"),
        payroll.hr.employee("Efe"),
        component_id=payroll.component("cooperative").id,
        amount="6,250",
        start_month=date(2026, 1, 1),
        end_month=date(2026, 3, 1),
        note="Thrift",
    )
    db.flush()
    run = payroll.run(1)
    service.calculate(db, payroll.hr.actor("Gbenga"), run)
    view = service.get_run(db, payroll.hr.actor("Ada"), run)
    efe = next(s for s in view.payslips if s.name == "Efe Test")
    codes = {line.code: line.amount for line in efe.lines}
    # 200,000 / 173 hours x 2 approved hours x 1.5; the pending 10 hours don't count.
    assert codes["overtime"] == D("3468.21")
    assert codes["cooperative"] == D(6250)
    assert efe.other_deductions == D(6250)


def _second_record(payroll: Payroll) -> AttendanceRecord:
    row = AttendanceRecord(
        employee_id=payroll.hr.employee("Efe"),
        work_date=date(2026, 1, 13),
        clock_in_at=datetime(2026, 1, 13, 7, 0, tzinfo=UTC),
        clock_out_at=datetime(2026, 1, 13, 20, 0, tzinfo=UTC),
    )
    payroll.hr.db.add(row)
    payroll.hr.db.flush()
    return row


def test_paye_counts_earlier_approved_months(payroll: Payroll) -> None:
    db = payroll.hr.db
    january = payroll.run(1)
    february = payroll.run(2)
    service.calculate(db, payroll.hr.actor("Gbenga"), february)
    view = service.get_run(db, payroll.hr.actor("Ada"), february)
    assert any("earlier run" in w for w in view.warnings)
    # Without January, February's PAYE covers two months of tax on one month's pay.
    # Tax on 317,000 through two twelfths of the bands: 183,666.67 at 15%.
    assert _slip(payroll, february, "Efe").paye == D("27550.00")
    payroll.through(january, last="approved")
    service.calculate(db, payroll.hr.actor("Gbenga"), february)
    slip = _slip(payroll, february, "Efe")
    assert (slip.paye, slip.ytd_paye, slip.ytd_taxable) == (D("39560.00"), D("79120.00"), D(634000))


def test_no_statutory_settings_no_run(payroll: Payroll) -> None:
    run = payroll.run(6, year=2025)
    with pytest.raises(Conflict, match="no statutory settings"):
        service.calculate(payroll.hr.db, payroll.hr.actor("Gbenga"), run)


# ------------------------------------------------------------------ maker-checker


def test_each_step_needs_a_different_person(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    run = payroll.run(1)
    service.calculate(db, a("Gbenga"), run)
    with pytest.raises(PermissionDenied, match="calculated this run"):
        service.review(db, a("Gbenga"), run)
    with pytest.raises(Conflict):
        service.approve(db, a("Ada"), run)  # not reviewed yet
    service.review(db, a("Ada"), run)  # a Finance Manager may review too
    with pytest.raises(PermissionDenied, match="reviewed this run"):
        service.approve(db, a("Ada"), run)
    with pytest.raises(PermissionDenied):
        service.approve(db, a("Femi"), run)  # payroll officers don't approve
    service.approve(db, a("Bola"), run)
    with pytest.raises(PermissionDenied, match="approved this run"):
        service.post(db, a("Bola"), run)
    view = service.get_run(db, a("Bola"), run)
    assert not view.can["post"] and "post" in view.blocked
    service.post(db, a("Ada"), run)
    with pytest.raises(PermissionDenied):
        service.mark_paid(db, a("Ada"), run)  # no run.pay
    service.mark_paid(db, a("Chi"), run)
    assert service.get_run(db, a("Chi"), run).status == "paid"
    with pytest.raises(Conflict):
        service.calculate(db, a("Gbenga"), run)


def test_the_creator_never_posts_or_pays(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    with pytest.raises(PermissionDenied):  # Finance Managers don't prepare runs
        service.create_run(db, a("Ada"), pay_group_id=payroll.group.id, year=2026, month=1)
    from app.identity.models import User

    bola = db.get(User, payroll.hr.org.users["Bola"])
    assert bola is not None
    payroll.hr.people.give(bola, "payroll_officer")
    run_id = service.create_run(db, a("Bola"), pay_group_id=payroll.group.id, year=2026, month=1)
    service.calculate(db, a("Gbenga"), run_id)
    service.review(db, a("Femi"), run_id)
    service.approve(db, a("Ada"), run_id)
    with pytest.raises(PermissionDenied, match="created this run"):
        service.post(db, a("Bola"), run_id)


def test_sending_back_needs_a_note(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    run = payroll.run(1)
    service.calculate(db, a("Gbenga"), run)
    service.review(db, a("Femi"), run)
    with pytest.raises(ValidationFailed):
        service.send_back(db, a("Ada"), run, note="no")
    service.send_back(db, a("Ada"), run, note="Efe's housing went up in January")
    view = service.get_run(db, a("Ada"), run)
    assert (view.status, view.returned_note) == ("draft", "Efe's housing went up in January")
    service.calculate(db, a("Gbenga"), run)
    service.delete_draft(db, a("Gbenga"), run)
    with pytest.raises(NotFound):
        service.get_run(db, a("Ada"), run)


# ------------------------------------------------------------------ payslips


def test_payslips_appear_to_employees_once_paid(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    run = payroll.run(1)
    payroll.through(run, last="approved")
    slip = _slip(payroll, run, "Efe")
    assert slip.file_id is not None
    assert service.my_payslips(db, a("Efe")) == []
    with pytest.raises(NotFound):
        files.open_for_download(db, a("Efe"), slip.file_id)
    with pytest.raises(Conflict, match="viruses"):  # payroll staff can open it already
        files.open_for_download(db, a("Gbenga"), slip.file_id)
    payroll.through(run)  # posted, then paid
    db.refresh(slip)
    mine = service.my_payslips(db, a("Efe"))
    assert [(p.period, p.net) for p in mine] == [("January 2026", D("277440.00"))]
    with pytest.raises(Conflict, match="viruses"):  # now Efe may open it (once scanned)
        files.open_for_download(db, a("Efe"), slip.file_id)
    with pytest.raises(NotFound):
        files.open_for_download(db, a("Hauwa"), slip.file_id)


def test_payslip_pdf_renders(payroll: Payroll) -> None:
    from app.modules.payroll import pdf

    data = pdf.render(
        pdf.PayslipDoc(
            organisation="Adeyemi Holdings Ltd",
            period="January 2026",
            pay_group="Staff",
            name="Efe Test",
            number="EMP-00005",
            pfa="Stanbic IBTC Pension",
            days_paid=16,
            days_in_month=31,
            earnings=[("Basic salary", "16 of 31 days", D("103225.81"))],
            deductions=[("PAYE", None, D("1000.00"))],
            gross=D("103225.81"),
            net=D("102225.81"),
            employer=[("Pension (employer)", D(1000))],
            ytd=[("PAYE", D(1000))],
        )
    )
    assert data.startswith(b"%PDF") and len(data) > 1000


# ------------------------------------------------------------------ rent relief and pay


def test_verified_rent_relief_lowers_paye(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    declaration = reliefs.declare(
        db,
        a("Efe"),
        tax_year=2026,
        annual_rent="1,800,000",
        filename="lease.pdf",
        declared_mime="application/pdf",
        data=PDF,
    )
    with pytest.raises(Conflict, match="already"):
        reliefs.declare(
            db,
            a("Efe"),
            tax_year=2026,
            annual_rent="1,000,000",
            filename="lease.pdf",
            declared_mime="application/pdf",
            data=PDF,
        )
    run = payroll.run(1)
    service.calculate(db, a("Gbenga"), run)
    assert _slip(payroll, run, "Efe").paye == D("39560.00")  # not verified yet
    assert [d.id for d in reliefs.queue(db, a("Dayo"), "pending")] == [declaration]
    with pytest.raises(ValidationFailed):
        reliefs.decide(db, a("Dayo"), declaration, verify=False, note="")
    reliefs.decide(db, a("Dayo"), declaration, verify=True)
    service.calculate(db, a("Gbenga"), run)
    slip = _slip(payroll, run, "Efe")
    assert (slip.rent_relief, slip.paye) == (D(30000), D("34160.00"))


def test_nobody_verifies_their_own_rent(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    own = reliefs.declare(
        db,
        a("Dayo"),
        tax_year=2026,
        annual_rent="900,000",
        filename="lease.pdf",
        declared_mime="application/pdf",
        data=PDF,
    )
    assert not reliefs.queue(db, a("Dayo"), "pending")[0].can_decide
    with pytest.raises(PermissionDenied, match="your own"):
        reliefs.decide(db, a("Dayo"), own, verify=True)
    with pytest.raises(PermissionDenied):
        reliefs.queue(db, a("Efe"), "pending")


def test_pay_changes_are_dated_and_never_your_own(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    payroll.pay(
        "Efe",
        {"basic": "250,000", "housing": "100,000", "transport": "50,000"},
        start=date(2026, 3, 1),
    )
    history = setup.pay_history(db, a("Femi"), payroll.hr.employee("Efe"))
    assert [(h.start_date, h.end_date, h.monthly_gross) for h in history] == [
        (date(2026, 3, 1), None, D(400000)),
        (date(2026, 1, 1), date(2026, 2, 28), D(350000)),
    ]
    with pytest.raises(Conflict):
        payroll.pay("Efe", start=date(2026, 2, 1))
    gbenga = payroll.hr.employee("Gbenga")
    with pytest.raises(PermissionDenied, match="your own"):
        setup.set_pay(
            db,
            a("Gbenga"),
            gbenga,
            pay_group_id=payroll.group.id,
            structure_id=None,
            start_date=date(2026, 1, 1),
            amounts={str(payroll.component("basic").id): "900,000"},
            reason="",
        )
    with pytest.raises(ValidationFailed) as exc:
        setup.set_pay(
            db,
            a("Gbenga"),
            payroll.hr.employee("Femi"),
            pay_group_id=None,
            structure_id=None,
            start_date=None,
            amounts={},
            reason="",
        )
    assert {"pay_group_id", "start_date", "form"} <= set(exc.value.field_errors)
    # Efe sees their own pay; colleagues don't.
    assert setup.pay_history(db, a("Efe"), payroll.hr.employee("Efe"))
    with pytest.raises(NotFound):
        setup.pay_history(db, a("Hauwa"), payroll.hr.employee("Efe"))
