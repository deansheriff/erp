"""Phase 10b: customers, quotes to invoices, VAT and stamp duty, credit notes issued by a
second person, receipts with WHT and allocation, reversals, aging, statements, reminders
and tax returns (ADR 0024)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.mail.models import MailOutbox
from app.modules.finance import (
    customers,
    journals,
    periods,
    receipts,
    receivables,
    sales,
    tax,
)
from app.modules.finance.customers import CustomerInput
from app.modules.finance.models import Journal, JournalLine, SalesDocument, WhtType
from app.modules.finance.sales import DocumentInput, LineInput
from tests.finance.conftest import Finance

pytestmark = pytest.mark.db

D = Decimal


def _customer(f: Finance, name: str = "Adebayo Foods Ltd", terms: str = "30") -> uuid.UUID:
    return customers.save(
        f.db,
        f.hr.actor("Ada"),
        None,
        CustomerInput(name, "accounts@adebayo.example", "", "", "", terms, True, True),
    )


def _line(f: Finance, description: str, qty: str, price: str, vat: str = "standard") -> LineInput:
    return LineInput(description, str(f.account("4100").id), "", qty, price, vat)


def _draft(
    f: Finance,
    who: str,
    kind: str,
    customer_id: uuid.UUID,
    day: date,
    lines: list[LineInput],
    *,
    stamp: str = "",
    document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    data = DocumentInput(kind, str(customer_id), day, None, None, "PO-77", "", stamp, lines)
    return sales.save_draft(f.db, f.hr.actor(who), document_id, data)


def _invoice(f: Finance, customer_id: uuid.UUID, day: date, *lines: LineInput) -> uuid.UUID:
    doc = _draft(f, "Chi", "invoice", customer_id, day, list(lines))
    sales.issue(f.db, f.hr.actor("Chi"), doc)
    return doc


def _journal_lines(f: Finance, journal_id: uuid.UUID | None) -> dict[str, tuple[Decimal, Decimal]]:
    rows = f.db.scalars(select(JournalLine).where(JournalLine.journal_id == journal_id))
    return {r.account_code: (r.debit, r.credit) for r in rows}


def _receipt(
    f: Finance,
    customer_id: uuid.UUID,
    day: date,
    cash: str,
    wht: str = "",
    allocations: list[tuple[uuid.UUID, str]] | None = None,
) -> uuid.UUID:
    wht_type = f.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    data = receipts.ReceiptInput(
        str(customer_id),
        day,
        str(f.account("1010").id),
        "TRF-1",
        cash,
        wht,
        str(wht_type.id) if wht else "",
        [(str(i), a) for i, a in allocations or []],
    )
    return receipts.record(f.db, f.hr.actor("Chi"), data)


# ------------------------------------------------------------------ invoices


def test_an_invoice_adds_vat_and_stamp_duty_and_posts_its_journal(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    assert tax.vat_rate_on(db, date(2026, 5, 1)) == D("7.50")  # seeded (confirm)
    duty = tax.save_stamp_type(
        db, a("Ada"), None, name="Agreement", basis="flat", value="500", is_active=True
    )
    who = _customer(finance)
    doc = _draft(
        finance,
        "Chi",
        "invoice",
        who,
        date(2026, 5, 10),
        [
            _line(finance, "Rice, 50kg bags", "10", "33,333.33"),
            _line(finance, "Export pallets", "1", "100,000", "zero"),
        ],
        stamp=str(duty),
    )
    number = sales.issue(db, a("Chi"), doc)  # whoever drafts an invoice may issue it
    assert number == "INV-00001"
    row = db.get(SalesDocument, doc)
    assert row is not None and row.status == "issued"
    assert row.due_date == date(2026, 6, 9)  # the customer's 30-day terms
    assert (row.net, row.vat, row.stamp_duty, row.total) == (
        D("433333.30"),
        D("25000.00"),  # 7.5% of 333,333.30, rounded per line
        D("500.00"),
        D("458833.30"),
    )
    lines = _journal_lines(finance, row.journal_id)
    assert lines["1100"] == (D("458833.30"), 0)
    assert lines["4100"] == (0, D("433333.30"))
    assert lines["2200"] == (0, D("25000.00"))
    assert lines["2220"] == (0, D("500.00"))
    journal = db.get(Journal, row.journal_id)
    assert journal is not None and journal.status == "posted" and journal.source == "receivable"
    with pytest.raises(Conflict):  # issued documents are never edited
        _draft(finance, "Chi", "invoice", who, date(2026, 5, 10), [], document_id=doc)
    with pytest.raises(Conflict):  # nor is their journal reversed from the Journals screen
        journals.reverse(db, a("Ada"), journal.id, on=date(2026, 5, 11))


def test_an_invoice_in_a_closed_or_unopened_month_is_refused(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    doc = _draft(
        finance, "Chi", "invoice", who, date(2025, 12, 20), [_line(finance, "Rice", "1", "1000")]
    )
    view = sales.get_document(db, a("Chi"), doc, today=date(2026, 1, 5))
    assert not view.can_issue and view.issue_blocked
    with pytest.raises(Conflict):
        sales.issue(db, a("Chi"), doc)
    assert db.get(SalesDocument, doc).number is None  # type: ignore[union-attr]


def test_quotes_and_orders_convert_and_come_back_if_the_draft_is_deleted(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    quote = _draft(
        finance, "Chi", "quote", who, date(2026, 6, 1), [_line(finance, "Catering", "2", "50,000")]
    )
    with pytest.raises(Conflict):
        sales.convert(db, a("Chi"), quote, "order")  # a draft quote isn't sent yet
    assert sales.issue(db, a("Chi"), quote) == "QT-00001"
    sales.set_status(db, a("Chi"), quote, "accept")
    order = sales.convert(db, a("Chi"), quote, "order")
    assert db.get(SalesDocument, quote).status == "converted"  # type: ignore[union-attr]
    sales.delete_draft(db, a("Chi"), order)
    assert db.get(SalesDocument, quote).status == "accepted"  # type: ignore[union-attr]
    order = sales.convert(db, a("Chi"), quote, "order")
    assert sales.issue(db, a("Chi"), order) == "SO-00001"
    invoice = sales.convert(db, a("Chi"), order, "invoice")
    assert db.get(SalesDocument, order).status == "invoiced"  # type: ignore[union-attr]
    sales.issue(db, a("Chi"), invoice)
    view = sales.get_document(db, a("Chi"), invoice, today=date(2027, 1, 31))
    assert view.source and view.source[2] == "SO-00001"
    assert view.total == D("107500.00") and view.shown_status == "overdue"


def test_a_credit_note_is_issued_by_someone_else_and_settles_its_invoice(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    invoice = _invoice(finance, who, date(2026, 7, 1), _line(finance, "Rice", "10", "10,000"))
    note = sales.credit_note_for(db, a("Ada"), invoice)
    _draft(
        finance,
        "Ada",
        "credit_note",
        who,
        date(2026, 7, 5),
        [_line(finance, "Rice returned", "2", "10,000")],
        document_id=note,
    )
    with pytest.raises(PermissionDenied):
        sales.issue(db, a("Chi"), note)  # officers can't issue credit notes
    with pytest.raises(PermissionDenied):
        sales.issue(db, a("Ada"), note)  # nor can the drafter
    assert sales.issue(db, a("Bola"), note) == "CN-00001"
    row = db.get(SalesDocument, note)
    assert row is not None and row.total == D("21500.00")
    lines = _journal_lines(finance, row.journal_id)
    assert lines["4100"] == (D("20000.00"), 0) and lines["2200"] == (D("1500.00"), 0)
    assert lines["1100"] == (0, D("21500.00"))
    view = sales.get_document(db, a("Chi"), invoice, today=date(2026, 7, 6))
    assert view.paid == D("21500.00") and view.shown_status == "part_paid"
    # Credit notes against an invoice can't come to more than it.
    big = sales.credit_note_for(db, a("Ada"), invoice)
    with pytest.raises(Conflict):
        sales.issue(db, a("Bola"), big)


# ------------------------------------------------------------------ receipts


def test_a_receipt_with_wht_settles_invoices_and_can_be_reversed(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    first = _invoice(finance, who, date(2026, 8, 1), _line(finance, "Rice", "1", "100,000"))
    second = _invoice(finance, who, date(2026, 8, 3), _line(finance, "Beans", "1", "40,000"))
    with pytest.raises(ValidationFailed) as exc:  # more than is left on the invoice
        _receipt(finance, who, date(2026, 8, 20), "200,000", allocations=[(first, "107,500.01")])
    assert f"alloc_{first}" in (exc.value.field_errors or {})
    with pytest.raises(ValidationFailed) as exc:  # WHT needs its type
        receipts.record(
            db,
            a("Chi"),
            receipts.ReceiptInput(
                str(who), date(2026, 8, 20), str(finance.account("1010").id), "", "1", "5", "", []
            ),
        )
    assert "wht_type_id" in (exc.value.field_errors or {})
    rid = _receipt(
        finance,
        who,
        date(2026, 8, 20),
        "112,125",
        "5,375",
        allocations=[(first, "107,500"), (second, "10,000")],
    )
    view = receipts.get_receipt(db, a("Chi"), rid)
    assert view.number == "RCT-00001" and view.total == D("117500.00")
    lines = _journal_lines(finance, view.journal[0] if view.journal else None)
    assert lines == {
        "1100": (0, D("117500.00")),
        "1010": (D("112125.00"), 0),
        "1320": (D("5375.00"), 0),
    }
    assert sales.get_document(db, a("Chi"), first, today=date(2026, 8, 21)).shown_status == "paid"
    # The reversal of a bounced payment reopens the invoices.
    with pytest.raises(PermissionDenied):
        receipts.reverse(db, a("Chi"), rid, on=date(2026, 8, 25), reason="Cheque bounced")
    receipts.reverse(db, a("Ada"), rid, on=date(2026, 8, 25), reason="Cheque bounced")
    assert sales.get_document(db, a("Chi"), first, today=date(2026, 8, 26)).paid == 0
    view = receipts.get_receipt(db, a("Chi"), rid)
    assert view.status == "reversed" and view.reversal is not None
    with pytest.raises(Conflict):
        receipts.reverse(db, a("Ada"), rid, on=date(2026, 8, 26), reason="Again, again")


def test_a_wrong_allocation_raises_instead_of_passing(finance: Finance) -> None:
    db = finance.db
    who = _customer(finance)
    first = _invoice(finance, who, date(2026, 8, 1), _line(finance, "Rice", "1", "100,000"))
    with pytest.raises(ValidationFailed) as exc:
        _receipt(finance, who, date(2026, 8, 2), "1,000", allocations=[(first, "2,000")])
    assert "form" in (exc.value.field_errors or {}) or f"alloc_{first}" in (
        exc.value.field_errors or {}
    )
    assert db.scalar(select(SalesDocument.id).where(SalesDocument.id == first))


def test_credit_is_allocated_later_and_wht_certificates_are_recorded(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    rid = _receipt(finance, who, date(2026, 9, 1), "47,500", "2,500")  # paid in advance
    credit = receivables.credits(db, who)
    assert [(c.number, c.remaining) for c in credit] == [("RCT-00001", D("50000.00"))]
    invoice = _invoice(finance, who, date(2026, 9, 5), _line(finance, "Rice", "1", "40,000"))
    with pytest.raises(ValidationFailed):
        receipts.allocate(
            db,
            a("Chi"),
            who,
            credit=f"receipt:{rid}",
            invoice_id=str(invoice),
            amount="43,000.01",
            today=date(2026, 9, 6),
        )
    receipts.allocate(
        db,
        a("Chi"),
        who,
        credit=f"receipt:{rid}",
        invoice_id=str(invoice),
        amount="43,000",
        today=date(2026, 9, 6),
    )
    summary = receivables.customer_summary(db, who, today=date(2026, 9, 6))
    assert summary.balance == D("-7000.00") and summary.unallocated == D("7000.00")
    assert summary.wht_awaiting == D("2500.00")
    receipts.record_certificate(
        db, a("Chi"), rid, number="FIRS/WHT/99", received_on=date(2026, 10, 1)
    )
    assert receivables.customer_summary(db, who, today=date(2026, 10, 1)).wht_awaiting == 0


# ------------------------------------------------------------------ aging, statements, reminders


def test_aging_buckets_and_the_statement(finance: Finance) -> None:
    db = finance.db
    who = _customer(finance, terms="0")
    old = _invoice(finance, who, date(2026, 6, 1), _line(finance, "Rice", "1", "10,000", "exempt"))
    _invoice(finance, who, date(2026, 8, 20), _line(finance, "Beans", "1", "20,000", "exempt"))
    _invoice(finance, who, date(2026, 9, 30), _line(finance, "Oil", "1", "30,000", "exempt"))
    _receipt(finance, who, date(2026, 9, 2), "4,000", allocations=[(old, "4,000")])
    report = receivables.aging(db, as_at=date(2026, 9, 30))
    (row,) = report.rows
    # 6,000 left from 1 June (121 days), 20,000 from 20 August (41), 30,000 due today.
    assert row.buckets == [D("30000.00"), 0, D("20000.00"), 0, D("6000.00")]
    assert report.total == D("56000.00")
    customer = customers.get(db, finance.hr.actor("Chi"), who)
    statement = receivables.statement(db, customer, start=date(2026, 8, 1), end=date(2026, 9, 30))
    assert statement.opening == D("10000.00")
    assert [line.number for line in statement.lines] == ["INV-00002", "RCT-00001", "INV-00003"]
    assert statement.closing == D("56000.00")
    _, pdf = customers.statement_pdf(
        db, finance.hr.actor("Chi"), who, start=date(2026, 8, 1), end=date(2026, 9, 30)
    )
    assert pdf.startswith(b"%PDF")
    _, invoice_pdf = sales.document_pdf(db, finance.hr.actor("Chi"), old)
    assert invoice_pdf.startswith(b"%PDF")


def _reminders(f: Finance) -> int:
    return len(
        f.db.scalars(
            select(MailOutbox).where(MailOutbox.template == "finance_payment_reminder")
        ).all()
    )


def test_reminders_go_before_on_and_after_the_due_date_once_a_day(finance: Finance) -> None:
    db = finance.db
    who = _customer(finance, terms="10")
    _invoice(finance, who, date(2026, 10, 1), _line(finance, "Rice", "1", "10,000"))  # due 11 Oct
    assert customers.send_due(db, today=date(2026, 10, 7)) == 0
    assert customers.send_due(db, today=date(2026, 10, 8)) == 1  # 3 days before
    assert customers.send_due(db, today=date(2026, 10, 8)) == 0  # never twice in a day
    assert customers.send_due(db, today=date(2026, 10, 11)) == 1  # due date
    assert customers.send_due(db, today=date(2026, 10, 14)) == 0
    assert customers.send_due(db, today=date(2026, 10, 18)) == 1  # 7 days overdue
    assert _reminders(finance) == 3
    customers.remind_now(db, finance.hr.actor("Chi"), who, today=date(2026, 10, 19))
    with pytest.raises(Conflict):
        customers.remind_now(db, finance.hr.actor("Chi"), who, today=date(2026, 10, 19))
    assert _reminders(finance) == 4


# ------------------------------------------------------------------ tax


def test_wht_types_are_seeded_without_rates_and_settings_are_checked(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    types = tax.wht_types(db)
    assert len(types) == 10 and all(t.company_rate is None for t in types)
    rows = [tax.WhtRateInput(str(t.id), "", "", True) for t in types]
    rows[0] = tax.WhtRateInput(str(types[0].id), "120", "", True)
    with pytest.raises(ValidationFailed):
        tax.save_wht_rates(db, a("Ada"), rows)
    rows[0] = tax.WhtRateInput(str(types[0].id), "2", "2", True)
    with pytest.raises(PermissionDenied):
        tax.save_wht_rates(db, a("Chi"), rows)
    tax.save_wht_rates(db, a("Ada"), rows)
    assert tax.wht_types(db)[0].company_rate == D("2.00")
    data = tax.SettingsInput(
        "4100", "2200", "1310", "1320", "2210", "2220", "45", "3", True, "7", "21"
    )
    with pytest.raises(ValidationFailed) as exc:  # receivables must be an asset
        tax.save_settings(db, a("Ada"), data)
    assert "receivables_code" in (exc.value.field_errors or {})
    tax.save_settings(
        db,
        a("Ada"),
        tax.SettingsInput(
            "1100", "2200", "1310", "1320", "2210", "2220", "45", "3", True, "7", "21"
        ),
    )
    assert periods.settings(db).default_terms_days == 45


def test_the_vat_return_reads_the_ledger_and_is_frozen_when_filed(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _customer(finance)
    _invoice(finance, who, date(2026, 10, 5), _line(finance, "Rice", "1", "200,000"))
    finance.posted(date(2026, 10, 20), [("1310", "3,000", ""), ("1010", "", "3,000")], "Input VAT")
    today = date(2026, 11, 22)
    rows = tax.returns(db, a("Chi"), "vat", today=today)
    assert rows[0].label == "October 2026" and rows[0].due == date(2026, 11, 21)
    assert (rows[0].output, rows[0].input, rows[0].net) == (
        D("15000.00"),
        D("3000.00"),
        D("12000.00"),
    )
    assert rows[0].status == "draft" and rows[0].overdue
    with pytest.raises(PermissionDenied):
        tax.file_return(
            db, a("Chi"), "vat", date(2026, 10, 1), filed_on=today, reference="X", today=today
        )
    with pytest.raises(Conflict):  # the month hasn't ended
        tax.file_return(
            db, a("Ada"), "vat", date(2026, 11, 1), filed_on=today, reference="X", today=today
        )
    with pytest.raises(Conflict):
        tax.mark_paid(db, a("Ada"), "vat", date(2026, 10, 1), paid_on=today, reference="P")
    tax.file_return(
        db, a("Ada"), "vat", date(2026, 10, 1), filed_on=today, reference="TaxPro-881", today=today
    )
    finance.posted(date(2026, 10, 31), [("1010", "750", ""), ("2200", "", "750")], "Late VAT")
    row, outputs, _ = tax.return_detail(db, a("Chi"), "vat", date(2026, 10, 1), today=today)
    assert row.status == "filed" and row.net == D("12000.00")  # as filed
    assert row.changed_by == D("750.00") and len(outputs) == 2
    tax.mark_paid(db, a("Ada"), "vat", date(2026, 10, 1), paid_on=today, reference="RRR-1")
    assert tax.returns(db, a("Chi"), "vat", today=today)[0].status == "paid"
