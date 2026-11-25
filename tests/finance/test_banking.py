"""Phase 10d: bank accounts with their own ledger accounts, statement import from CSV,
Excel and OFX, auto-matching and rules, reconciliation to a ₦0.00 difference approved by
a second person, and transfers (ADR 0026)."""

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.modules.finance import banking, journals, reconciliations, statements, transfers
from app.modules.finance.banking import BankAccountInput, RuleInput
from app.modules.finance.models import (
    Account,
    BankAccount,
    BankTransfer,
    JournalLine,
    StatementLine,
)
from tests.finance.conftest import Finance

pytestmark = pytest.mark.db

D = Decimal
CSV = (
    "Date,Description,Reference,Debit,Credit\r\n"
    '03/10/2026,TRF FROM ADEBAYO FOODS,FT1,,"500,000.00"\r\n'
    '05/10/2026,CHQ 0012 LANDLORD,CHQ12,"100,000.00",\r\n'
    "06/10/2026,SMS ALERT CHARGES,,52.50,\r\n"
    '07/10/2026,INTEREST PAID,,,"1,000.00"\r\n'
)


def _main(f: Finance) -> BankAccount:
    account = f.account("1010")
    return f.db.scalars(select(BankAccount).where(BankAccount.account_id == account.id)).one()


def _layout(name: str = "GTBank · Operations", **overrides: str) -> BankAccountInput:
    fields = {
        "name": name,
        "bank_name": "GTBank",
        "bank_code": "058",
        "account_number": "0123454821",
        "account_name": "Example Ltd",
        "csv_date": "Date",
        "csv_description": "Description",
        "csv_reference": "Reference",
        "csv_amount": "",
        "csv_money_in": "Credit",
        "csv_money_out": "Debit",
        "csv_date_format": "%d/%m/%Y",
        "csv_skip_rows": "0",
    }
    fields.update(overrides)
    return BankAccountInput(**fields, is_active=True)


def test_a_bank_account_gets_its_own_ledger_account(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    rows = banking.list_accounts(db, a("Chi"))
    assert [r.code for r in rows] == ["1010"]  # the seeded one, details to fill in
    with pytest.raises(PermissionDenied):
        banking.save_account(db, a("Chi"), None, _layout())
    with pytest.raises(ValidationFailed) as exc:
        banking.save_account(db, a("Ada"), None, _layout(csv_money_in="", account_number="123"))
    assert {"csv_amount", "account_number"} <= set(exc.value.field_errors or {})
    bank_id = banking.save_account(db, a("Ada"), None, _layout())
    ledger = banking.ledger_of(db, bank_id)
    header = finance.account("10")
    assert (ledger.code, ledger.type, ledger.cash_flow, ledger.parent_id) == (
        "1011",
        "asset",
        "cash",
        header.id,
    )


def test_import_matches_skips_repeats_and_applies_rules(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    bank = _main(finance)
    banking.save_account(db, a("Ada"), bank.id, _layout("Bank: main account"))
    banking.save_rule(
        db,
        a("Ada"),
        None,
        RuleInput("", "sms alert", "out", str(finance.account("6460").id), "", "SMS charges", True),
    )
    finance.posted(date(2026, 10, 2), [("1010", "500,000", ""), ("4100", "", "500,000")], "Sale")
    finance.posted(date(2026, 10, 5), [("6400", "100,000", ""), ("1010", "", "100,000")], "Rent")
    finance.posted(date(2026, 10, 30), [("6400", "20,000", ""), ("1010", "", "20,000")], "Repairs")
    result = statements.import_statement(
        db, a("Chi"), bank.id, filename="oct.csv", data=CSV.encode(), mime="text/csv"
    )
    assert (result.added, result.skipped, result.matched, result.ruled) == (4, 0, 2, 1)
    again = statements.import_statement(
        db, a("Chi"), bank.id, filename="oct-again.csv", data=CSV.encode(), mime="text/csv"
    )
    assert (again.added, again.skipped) == (0, 4)
    (interest,) = statements.lines_for(db, a("Chi"), bank.id, "unmatched")
    assert interest.amount == D("1000.00") and interest.suggestions == []
    statements.give_entry(
        db,
        a("Chi"),
        interest.id,
        account_id=str(finance.account("4900").id),
        cost_centre_id="",
        description="Interest",
    )

    # The statement closes at 400,947.50; the 20,000 repair isn't on it yet.
    with pytest.raises(ValidationFailed):
        reconciliations.start(db, a("Chi"), bank.id, statement_date=None, balance="x")
    rec = reconciliations.start(
        db, a("Chi"), bank.id, statement_date=date(2026, 10, 31), balance="400,000.00"
    )
    with pytest.raises(Conflict):  # out by 947.50
        reconciliations.submit(db, a("Chi"), rec)
    reconciliations.start(
        db, a("Chi"), bank.id, statement_date=date(2026, 10, 31), balance="400,947.50"
    )
    view = reconciliations.get(db, a("Chi"), rec)
    assert view.figures.ledger == D("380000.00")
    assert view.figures.outstanding_total == D("-20000.00")
    assert view.figures.pending_total == D("947.50")
    assert view.figures.difference == 0
    reconciliations.submit(db, a("Chi"), rec)
    with pytest.raises(PermissionDenied):
        reconciliations.approve(db, a("Chi"), rec)  # officers prepare, managers approve
    reconciliations.approve(db, a("Ada"), rec)
    assert banking.ledger_balance(db, bank.account_id) == D("380947.50")
    charges = db.scalars(select(JournalLine).where(JournalLine.account_code == "6460")).one()
    assert charges.debit == D("52.50")
    locked = db.scalars(select(StatementLine).where(StatementLine.status == "matched")).first()
    assert locked is not None and locked.reconciliation_id is not None
    with pytest.raises(Conflict):
        statements.unmatch(db, a("Chi"), locked.id)
    with pytest.raises(ValidationFailed):  # the next one is dated after the last
        reconciliations.start(db, a("Chi"), bank.id, statement_date=date(2026, 10, 15), balance="1")


def test_matching_by_hand(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    bank = _main(finance)
    banking.save_account(db, a("Ada"), bank.id, _layout("Bank: main account"))
    # Two ledger lines for the same amount: auto-matching leaves the choice to a person.
    for day in (2, 4):
        finance.posted(
            date(2026, 10, day), [("1010", "500,000", ""), ("4100", "", "500,000")], "Sale"
        )
    csv = 'Date,Description,Reference,Debit,Credit\r\n03/10/2026,TRF IN,,,"500,000.00"\r\n'
    result = statements.import_statement(
        db, a("Chi"), bank.id, filename="a.csv", data=csv.encode(), mime="text/csv"
    )
    assert result.matched == 0
    (line,) = statements.lines_for(db, a("Chi"), bank.id, "unmatched")
    assert len(line.suggestions) == 2
    statements.match(db, a("Chi"), line.id, line.suggestions[1].journal_line_id)
    with pytest.raises(Conflict):
        statements.match(db, a("Chi"), line.id, line.suggestions[0].journal_line_id)
    statements.unmatch(db, a("Chi"), line.id)
    assert statements.counts(db, bank.id)["unmatched"] == 1


def test_excel_and_ofx_statements_parse(finance: Finance) -> None:
    bank = _main(finance)
    banking.save_account(
        finance.db, finance.hr.actor("Ada"), bank.id, _layout("Main", csv_skip_rows="1")
    )
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["GTBank statement for October"])
    sheet.append(["Date", "Description", "Reference", "Debit", "Credit"])
    sheet.append([date(2026, 10, 3), "TRF IN", "FT9", None, 250000])
    sheet.append([date(2026, 10, 4), "POS", None, 1200.5, None])
    data = io.BytesIO()
    book.save(data)
    lines, problems = statements.parse(bank, "oct.xlsx", data.getvalue())
    assert problems == []
    assert [(x.line_date, x.amount) for x in lines] == [
        (date(2026, 10, 3), D("250000.00")),
        (date(2026, 10, 4), D("-1200.50")),
    ]
    ofx = (
        "OFXHEADER:100\nDATA:OFXSGML\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261006120000<TRNAMT>-52.50<FITID>A1"
        "<NAME>SMS ALERT<MEMO>October</STMTTRN>"
        "<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261007<TRNAMT>1000.00<FITID>A2<NAME>INTEREST"
        "</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    lines, problems = statements.parse(bank, "oct.ofx", ofx.encode())
    assert problems == []
    assert [(x.description, x.amount, x.reference) for x in lines] == [
        ("SMS ALERT October", D("-52.50"), "A1"),
        ("INTEREST", D("1000.00"), "A2"),
    ]
    _, problems = statements.parse(bank, "bad.csv", b"When,What\r\nx,y\r\n")
    assert "aren't in the file's header" in problems[0]


def test_transfers_are_approved_by_someone_else(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    main = _main(finance)
    other = banking.save_account(db, a("Ada"), None, _layout())
    with pytest.raises(ValidationFailed):
        transfers.record(
            db,
            a("Chi"),
            from_id=str(main.id),
            to_id=str(main.id),
            amount="1",
            on=date(2026, 10, 1),
            reference="",
        )
    tid = transfers.record(
        db,
        a("Chi"),
        from_id=str(main.id),
        to_id=str(other),
        amount="50,000",
        on=date(2026, 10, 1),
        reference="Float",
    )
    with pytest.raises(PermissionDenied):
        transfers.approve(db, a("Chi"), tid)
    assert transfers.approve(db, a("Ada"), tid) == "TRF-00001"
    row = db.get(BankTransfer, tid)
    assert row is not None and row.journal_id is not None
    lines = {
        jl.account_code: (jl.debit, jl.credit)
        for jl in db.scalars(select(JournalLine).where(JournalLine.journal_id == row.journal_id))
    }
    assert lines == {"1011": (D("50000.00"), 0), "1010": (0, D("50000.00"))}
    with pytest.raises(Conflict):  # banking journals are corrected by a manual journal
        journals.reverse(db, a("Ada"), row.journal_id, on=date(2026, 10, 2))
    assert db.scalars(select(Account).where(Account.code == "1011")).one().cash_flow == "cash"
