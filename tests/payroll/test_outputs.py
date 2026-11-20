"""Phase 9b: loans repaid through payroll, journals queued for the ledger, remittance
files and year-end tax certificates (ADR 0022)."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.core.secrets import encrypt
from app.modules.hr.models import Employee
from app.modules.payroll import certificates, ledger, loans, remittances
from app.modules.payroll.models import PayrollJournal, PayrollJournalLine, Payslip, PayslipLine
from tests.payroll.conftest import Payroll

pytestmark = pytest.mark.db

NOV, DEC = date(2026, 11, 1), date(2026, 12, 1)


def _loan(
    p: Payroll, who: str = "Efe", amount: str = "100,000", instalment: str = "40,000"
) -> object:
    return loans.record(
        p.hr.db,
        p.hr.actor("Gbenga"),
        employee_id=p.hr.employee(who),
        kind="loan",
        amount=amount,
        instalment=instalment,
        first_month=NOV,
        note="School fees",
    )


def _bank_details(p: Payroll, who: str, bank: str | None, account: str | None) -> None:
    row = p.hr.db.get(Employee, p.hr.employee(who))
    assert row is not None
    row.bank_name = bank
    row.account_number_encrypted = encrypt(account) if account else None
    row.account_name = f"{who} Account"
    row.state_of_residence = "Lagos" if who == "Efe" else None
    row.pension_pfa = "Stanbic IBTC Pension Managers"
    row.rsa_pin_encrypted = encrypt("PEN100200300400")
    row.tin_encrypted = encrypt("12345678-0001")
    p.hr.db.flush()


def _journal(p: Payroll, kind: str) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    db = p.hr.db
    journal = db.scalars(select(PayrollJournal).where(PayrollJournal.kind == kind)).one()
    lines = list(
        db.scalars(select(PayrollJournalLine).where(PayrollJournalLine.journal_id == journal.id))
    )
    by_account: dict[str, Decimal] = {}
    for line in lines:
        by_account[line.account] = (
            by_account.get(line.account, Decimal(0)) + line.debit - line.credit
        )
    return sum(x.debit for x in lines), sum(x.credit for x in lines), by_account


# ------------------------------------------------------------------ loans


def test_recording_a_loan_checks_the_numbers(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    with pytest.raises(ValidationFailed) as exc:
        loans.record(
            db,
            a("Gbenga"),
            employee_id=payroll.hr.employee("Efe"),
            kind="loan",
            amount="50,000",
            instalment="60,000",
            first_month=date(2026, 10, 1),
        )
    assert set(exc.value.field_errors or {}) == {"instalment", "first_month"}
    with pytest.raises(PermissionDenied):
        loans.record(
            db,
            a("Ada"),
            employee_id=payroll.hr.employee("Efe"),
            kind="loan",
            amount="50,000",
            instalment="10,000",
            first_month=NOV,
        )


def test_finance_approves_but_not_the_recorder_or_borrower(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    loan_id = loans.record(
        db,
        a("Gbenga"),
        employee_id=payroll.hr.employee("Ada"),
        kind="advance",
        amount="30,000",
        instalment="30,000",
        first_month=NOV,
    )
    with pytest.raises(PermissionDenied):
        loans.decide(db, a("Ada"), loan_id, approve=True)  # her own advance
    with pytest.raises(ValidationFailed):
        loans.decide(db, a("Bola"), loan_id, approve=False, note="")
    loans.decide(db, a("Bola"), loan_id, approve=True)
    [view] = loans.list_loans(db, a("Bola"), "repaying")
    assert view.number == "LN-00001" and view.kind_label == "Salary advance"
    debit, credit, accounts = _journal(payroll, "loan")
    assert debit == credit == Decimal("30000.00")
    assert accounts == {"1420": Decimal("30000.00"), "1010": Decimal("-30000.00")}
    with pytest.raises(Conflict):
        loans.withdraw(db, a("Gbenga"), loan_id)


def test_instalments_come_off_pay_until_cleared(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    loan_id = _loan(payroll)
    assert loans.due(db, [payroll.hr.employee("Efe")], NOV) == {}  # not approved yet
    loans.decide(db, a("Ada"), loan_id, approve=True)

    november = payroll.run(11)
    payroll.through(november, "approved")
    line = db.scalars(select(PayslipLine).where(PayslipLine.loan_id == loan_id)).one()
    assert line.amount == Decimal("40000.00") and line.name == "Loan repayment"
    [view] = loans.list_loans(db, a("Ada"), "repaying")
    assert view.repaid == Decimal("40000.00") and view.balance == Decimal("60000.00")
    assert [(m.month, m.amount, m.repaid) for m in view.schedule] == [
        (NOV, Decimal("40000.00"), True),
        (DEC, Decimal("40000.00"), False),
        (date(2027, 1, 1), Decimal("20000.00"), False),
    ]

    december = payroll.run(12)
    payroll.through(december, "approved")
    assert loans.due(db, [payroll.hr.employee("Efe")], date(2027, 1, 1))[
        payroll.hr.employee("Efe")
    ][0][1] == Decimal("20000.00")
    mine = loans.mine(db, a("Efe"))
    assert mine[0].balance == Decimal("20000.00") and mine[0].status == "repaying"


def test_a_pending_loan_can_be_withdrawn(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    loan_id = _loan(payroll)
    loans.withdraw(db, a("Femi"), loan_id)
    assert loans.list_loans(db, a("Ada"), "pending") == []
    assert [v.status for v in loans.list_loans(db, a("Ada"), "rejected")] == ["cancelled"]


# ------------------------------------------------------------------ journals


def test_posting_and_paying_queue_balanced_journals(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    loans.decide(db, a("Ada"), _loan(payroll), approve=True)
    run = payroll.run(11)
    payroll.through(run, "posted")
    slips = list(db.scalars(select(Payslip).where(Payslip.run_id == run)))
    gross = sum(s.gross for s in slips)
    net = sum(s.net for s in slips)
    debit, credit, accounts = _journal(payroll, "accrual")
    assert debit == credit
    assert accounts["6000"] == gross
    assert accounts["1420"] == Decimal("-40000.00")  # the loan book is repaid
    assert accounts["2170"] == -net
    assert accounts.get("2110", Decimal(0)) == -sum(s.paye for s in slips)

    payroll.through(run, "paid")
    debit, credit, accounts = _journal(payroll, "payment")
    assert debit == credit == net
    assert accounts == {"2170": net, "1010": -net}
    view = remittances.summary(db, a("Chi"), run)
    assert [j.kind for j in view.journals] == ["accrual", "payment"]


def test_ledger_accounts_and_banks_are_settings(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    values = {name: ledger.accounts(db).__getattribute__(name) for name, _ in ledger.ACCOUNT_FIELDS}
    with pytest.raises(PermissionDenied):
        ledger.save_accounts(db, a("Gbenga"), values)
    with pytest.raises(ValidationFailed):
        ledger.save_accounts(db, a("Ada"), {**values, "salaries": "bad code!"})
    ledger.save_accounts(db, a("Ada"), {**values, "salaries": "6001"})
    assert ledger.accounts(db).salaries == "6001"
    match = ledger.bank_matcher(db)
    assert match("GTBank").code == "058"  # type: ignore[union-attr]
    assert match("guaranty trust bank plc").code == "058"  # type: ignore[union-attr]
    assert match("First Bank").code == "011"  # type: ignore[union-attr]
    assert match("Bank of Nowhere") is None
    ledger.save_bank(db, a("Ada"), None, name="Bank of Nowhere", code="999", other_names="BoN")
    assert ledger.bank_matcher(db)("BoN").code == "999"  # type: ignore[union-attr]


# ------------------------------------------------------------------ remittances


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_remittance_files(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    _bank_details(payroll, "Efe", "Zenith Bank Plc", "0123456789")
    _bank_details(payroll, "Hauwa", "Bank of Nowhere", "9876543210")
    senior = {"basic": "2,000,000", "housing": "500,000", "transport": "200,000"}
    payroll.pay("Efe", senior, start=NOV)  # enough for PAYE in a first month
    payroll.pay("Hauwa", senior, start=NOV)
    run = payroll.run(11)
    payroll.through(run, "calculated")
    with pytest.raises(Conflict):
        remittances.summary(db, a("Chi"), run)  # not approved yet
    payroll.through(run, "approved")

    view = remittances.summary(db, a("Chi"), run)
    assert view.bank_people == 1 and view.people == 2
    assert [why for _, why in view.left_out] == ["Bank not on the list: Bank of Nowhere"]
    assert [g.label for g in view.paye] == ["Lagos", "Not set"]
    assert [g.label for g in view.pension] == ["Stanbic IBTC Pension Managers"]

    name, text = remittances.bank_file(db, a("Chi"), run)
    rows = _rows(text)
    assert name == "bank-2026-11.csv"
    assert rows[0][:3] == ["Account name", "Account number", "Bank code"]
    assert rows[1][:4] == ["Efe Account", "0123456789", "057", "Zenith Bank"]
    _, text = remittances.paye_schedule(db, a("Chi"), run, "Lagos")
    assert [r[0] for r in _rows(text)[1:]] == ["Lagos"] and _rows(text)[1][3] == "12345678-0001"
    _, text = remittances.pension_schedule(db, a("Chi"), run)
    assert {r[3] for r in _rows(text)[1:]} == {"PEN100200300400"}
    _, text = remittances.nhf_schedule(db, a("Chi"), run)
    assert len(_rows(text)) == 3
    with pytest.raises(PermissionDenied):
        remittances.bank_file(db, a("Efe"), run)


def test_csv_cells_never_start_a_formula() -> None:
    assert remittances._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert remittances._cell("@SUM") == "'@SUM"
    assert remittances._cell("Efe") == "Efe"


# ------------------------------------------------------------------ tax certificates


def test_certificates_wait_for_december_for_employees(payroll: Payroll) -> None:
    db, a = payroll.hr.db, payroll.hr.actor
    payroll.through(payroll.run(11), "paid")
    view = certificates.year_view(db, a("Gbenga"), 2026)
    assert view.year == 2026 and not view.december_paid
    assert [r.months for r in view.rows] == [1, 1]
    name, body = certificates.certificate(db, a("Gbenga"), payroll.hr.employee("Efe"), 2026)
    assert body.startswith(b"%PDF") and name.startswith("tax-certificate-2026-")
    assert certificates.my_years(db, a("Efe")) == []
    with pytest.raises(Conflict):
        certificates.certificate(db, a("Efe"), payroll.hr.employee("Efe"), 2026)

    payroll.through(payroll.run(12), "paid")
    assert certificates.year_view(db, a("Gbenga"), 2026).december_paid
    assert certificates.my_years(db, a("Efe")) == [2026]
    _, body = certificates.certificate(db, a("Efe"), payroll.hr.employee("Efe"), 2026)
    assert body.startswith(b"%PDF")
    with pytest.raises(Exception):  # noqa: B017  (someone else's is out of scope: 404)
        certificates.certificate(db, a("Efe"), payroll.hr.employee("Hauwa"), 2026)
