"""Phase 10c: suppliers, purchase orders approved by a second person, goods received, bills
matched three ways (held when they don't match), debit notes, payments with WHT approved
by a second person, the bank file, reversal and aging (ADR 0025)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.modules.finance import (
    bills,
    journals,
    payables,
    payments,
    purchasing,
    sales,
    suppliers,
    tax,
)
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.models import Bill, JournalLine, PurchaseOrder, WhtType
from app.modules.finance.suppliers import SupplierInput
from tests.finance.conftest import Finance

pytestmark = pytest.mark.db

D = Decimal


def _supplier(
    f: Finance, name: str = "Lagos Office Supplies Ltd", *, bank: bool = False
) -> uuid.UUID:
    wht = f.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    return suppliers.save(
        f.db,
        f.hr.actor("Ada"),
        None,
        SupplierInput(
            name,
            "company",
            "accounts@los.example",
            "",
            "",
            "12345678-0001",
            "30",
            str(wht.id),
            "GTBank" if bank else "",
            "058" if bank else "",
            "0123454821" if bank else "",
            name if bank else "",
            True,
        ),
    )


def _po(f: Finance, supplier_id: uuid.UUID, who: str = "Chi") -> uuid.UUID:
    line = sales.LineInput(
        "A4 paper, boxes", str(f.account("6300").id), "", "10", "10,000", "standard"
    )
    data = purchasing.OrderInput(str(supplier_id), date(2026, 9, 1), None, "", "", [line])
    return purchasing.save_order(f.db, f.hr.actor(who), None, data)


def _lines(f: Finance, journal_id: uuid.UUID | None) -> dict[str, tuple[Decimal, Decimal]]:
    rows = f.db.scalars(select(JournalLine).where(JournalLine.journal_id == journal_id))
    return {r.account_code: (r.debit, r.credit) for r in rows}


def _set_bill(
    f: Finance,
    bill_id: uuid.UUID,
    reference: str,
    *,
    price: str | None = None,
    qty: str | None = None,
) -> None:
    view = bills.get_bill(f.db, f.hr.actor("Chi"), bill_id, today=date(2026, 9, 20))
    data = BillInput(
        "bill",
        str(view.supplier_id),
        reference,
        date(2026, 9, 15),
        None,
        str(view.wht_type_id or ""),
        "",
        [
            BillLineInput(
                str(line.order_line_id or ""),
                line.description,
                str(line.account_id),
                "",
                qty or f"{line.quantity}",
                price or f"{line.unit_price}",
                line.vat_code,
            )
            for line in view.lines
        ],
    )
    bills.save_bill(f.db, f.hr.actor("Chi"), bill_id, data)


def _direct_bill(
    f: Finance, supplier_id: uuid.UUID, reference: str, amount: str, day: date
) -> uuid.UUID:
    wht = f.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    data = BillInput(
        "bill",
        str(supplier_id),
        reference,
        day,
        None,
        str(wht.id),
        "",
        [
            BillLineInput(
                "", "Office rent", str(finance_account(f, "6400")), "", "1", amount, "standard"
            )
        ],
    )
    return bills.save_bill(f.db, f.hr.actor("Chi"), None, data)


def finance_account(f: Finance, code: str) -> uuid.UUID:
    return f.account(code).id


# ------------------------------------------------------------------ orders and receiving


def test_a_purchase_order_is_approved_by_someone_else_and_received(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _supplier(finance)
    order = _po(finance, who, "Ada")
    with pytest.raises(PermissionDenied):
        purchasing.approve_order(db, a("Chi"), order)  # officers draft only
    with pytest.raises(PermissionDenied):
        purchasing.approve_order(db, a("Ada"), order)  # nor does the drafter approve
    assert purchasing.approve_order(db, a("Bola"), order) == "PO-00001"
    row = db.get(PurchaseOrder, order)
    assert row is not None and row.total == D("107500.00")
    lines = purchasing.order_lines(db, order)
    with pytest.raises(ValidationFailed) as exc:
        purchasing.receive(
            db,
            a("Chi"),
            order,
            received_on=date(2026, 9, 5),
            delivery_note="DN-1",
            note="",
            quantities={str(lines[0].id): "11"},
        )
    assert f"qty_{lines[0].id}" in (exc.value.field_errors or {})
    assert (
        purchasing.receive(
            db,
            a("Chi"),
            order,
            received_on=date(2026, 9, 5),
            delivery_note="DN-1",
            note="",
            quantities={str(lines[0].id): "8"},
        )
        == "GRN-00001"
    )
    view = purchasing.get_order(db, a("Chi"), order)
    assert view.shown_status == "part_received" and view.lines[0].progress.to_receive == 2
    with pytest.raises(Conflict):
        purchasing.cancel_order(db, a("Bola"), order)
    assert db.scalars(select(JournalLine)).first() is None  # receiving posts nothing


# ------------------------------------------------------------------ bills


def test_a_matched_bill_posts_and_a_mismatched_one_is_held(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _supplier(finance)
    order = _po(finance, who)
    purchasing.approve_order(db, a("Bola"), order)
    line = purchasing.order_lines(db, order)[0]
    purchasing.receive(
        db,
        a("Chi"),
        order,
        received_on=date(2026, 9, 5),
        delivery_note="",
        note="",
        quantities={str(line.id): "8"},
    )
    first = bills.bill_from_order(db, a("Chi"), order, today=date(2026, 9, 15))
    _set_bill(finance, first, "LOS/881")
    with pytest.raises(PermissionDenied):
        bills.approve(db, a("Chi"), first)
    assert bills.approve(db, a("Bola"), first) == "posted"
    row = db.get(Bill, first)
    assert row is not None and row.number == "BILL-00001" and row.total == D("86000.00")
    assert _lines(finance, row.journal_id) == {
        "6300": (D("80000.00"), 0),
        "1310": (D("6000.00"), 0),
        "2010": (0, D("86000.00")),
    }
    # The other 2 arrive; a bill for 2 at 5% over the PO price is held.
    purchasing.receive(
        db,
        a("Chi"),
        order,
        received_on=date(2026, 9, 18),
        delivery_note="",
        note="",
        quantities={str(line.id): "2"},
    )
    second = bills.bill_from_order(db, a("Chi"), order, today=date(2026, 9, 20))
    _set_bill(finance, second, "LOS/990", price="10500", qty="3")
    with pytest.raises(ValidationFailed):  # same supplier invoice number twice
        _set_bill(finance, second, "los/881")
    assert bills.approve(db, a("Bola"), second) == "held"
    held = db.get(Bill, second)
    assert held is not None and held.number is None
    assert "Billed 11, received 10" in (held.hold_reason or "")
    assert "5.0% above the PO" in (held.hold_reason or "")
    with pytest.raises(ValidationFailed):
        bills.approve(db, a("Ada"), second, reason="")
    assert (
        bills.approve(db, a("Ada"), second, reason="Agreed price rise, extra box kept") == "posted"
    )
    assert db.get(PurchaseOrder, order).status == "closed"  # type: ignore[union-attr]


def test_debit_notes_reduce_their_bill_and_are_issued_by_someone_else(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _supplier(finance)
    bill = _direct_bill(finance, who, "LOS/R-11", "200,000", date(2026, 10, 1))
    bills.approve(db, a("Bola"), bill)
    with pytest.raises(ValidationFailed):  # the same invoice number again
        _direct_bill(finance, who, "LOS/R-11", "1", date(2026, 10, 2))
    note = bills.debit_note_for(db, a("Ada"), bill, today=date(2026, 10, 5))
    data = BillInput(
        "debit_note",
        str(who),
        "",
        date(2026, 10, 5),
        None,
        "",
        "Overcharge",
        [
            BillLineInput(
                "",
                "Overcharge",
                str(finance_account(finance, "6400")),
                "",
                "1",
                "20,000",
                "standard",
            )
        ],
    )
    bills.save_bill(db, a("Ada"), note, data)
    with pytest.raises(PermissionDenied):
        bills.approve(db, a("Ada"), note)
    assert bills.approve(db, a("Bola"), note) == "posted"
    row = db.get(Bill, note)
    assert row is not None and row.number == "DN-00001" and row.total == D("21500.00")
    assert _lines(finance, row.journal_id) == {
        "2010": (D("21500.00"), 0),
        "6400": (0, D("20000.00")),
        "1310": (0, D("1500.00")),
    }
    view = bills.get_bill(db, a("Chi"), bill, today=date(2026, 10, 6))
    assert view.paid == D("21500.00") and view.shown_status == "part_paid"
    too_big = bills.debit_note_for(db, a("Ada"), bill, today=date(2026, 10, 6))
    with pytest.raises(Conflict):
        bills.approve(db, a("Bola"), too_big)
    with pytest.raises(Conflict):  # payables journals are reversed from their document
        journals.reverse(db, a("Ada"), row.journal_id, on=date(2026, 10, 7))  # type: ignore[arg-type]


# ------------------------------------------------------------------ payments


def _set_wht_rates(f: Finance, rate: str) -> None:
    rows = [
        tax.WhtRateInput(str(t.id), rate if t.key == "supply_of_goods" else "", "", True)
        for t in tax.wht_types(f.db)
    ]
    tax.save_wht_rates(f.db, f.hr.actor("Ada"), rows)


def test_a_payment_withholds_tax_and_is_approved_by_someone_else(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _supplier(finance)
    bill = _direct_bill(finance, who, "LOS/R-12", "100,000", date(2026, 10, 1))
    bills.approve(db, a("Bola"), bill)
    data = payments.PaymentInput(
        str(who), date(2026, 10, 20), str(finance.account("1010").id), "", [(str(bill), "107,500")]
    )
    with pytest.raises(ValidationFailed) as exc:  # no WHT rate entered yet
        payments.prepare(db, a("Chi"), None, data)
    assert "Enter it in Tax settings" in (exc.value.field_errors or {}).get(f"alloc_{bill}", "")
    _set_wht_rates(finance, "5")
    pid = payments.prepare(db, a("Chi"), None, data)
    view = payments.get_payment(db, a("Chi"), pid)
    assert (view.settled, view.wht, view.cash) == (D("107500.00"), D("5000.00"), D("102500.00"))
    with pytest.raises(PermissionDenied):
        payments.approve(db, a("Chi"), pid)
    assert payments.approve(db, a("Ada"), pid) == "PAY-00001"
    view = payments.get_payment(db, a("Chi"), pid)
    assert _lines(finance, view.journal[0] if view.journal else None) == {
        "2010": (D("107500.00"), 0),
        "1010": (0, D("102500.00")),
        "2210": (0, D("5000.00")),
    }
    assert bills.get_bill(db, a("Chi"), bill, today=date(2026, 10, 21)).shown_status == "paid"
    with pytest.raises(Conflict):  # no bank details on the supplier yet
        payments.bank_file(db, a("Ada"), pid)
    # WHT payable and VAT input now reach the tax returns.
    rows = {r.label: r for r in tax.returns(db, a("Ada"), "wht", today=date(2026, 11, 22))}
    assert rows["October 2026"].output == D("5000.00")
    rows = {r.label: r for r in tax.returns(db, a("Ada"), "vat", today=date(2026, 11, 22))}
    assert rows["October 2026"].input == D("7500.00")
    payments.reverse(db, a("Ada"), pid, on=date(2026, 10, 25), reason="Sent to the wrong account")
    assert bills.get_bill(db, a("Chi"), bill, today=date(2026, 10, 26)).paid == 0


def test_the_bank_file_and_aging(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    who = _supplier(finance, bank=True)
    _set_wht_rates(finance, "5")
    old = _direct_bill(finance, who, "A-1", "10,000", date(2026, 6, 1))
    new = _direct_bill(finance, who, "A-2", "20,000", date(2026, 9, 1))
    for bill in (old, new):
        bills.approve(db, a("Bola"), bill)
    pid = payments.prepare(
        db,
        a("Chi"),
        None,
        payments.PaymentInput(
            str(who),
            date(2026, 9, 10),
            str(finance.account("1010").id),
            "Sept run",
            [(str(old), "5,375")],
        ),
    )
    payments.approve(db, a("Ada"), pid)
    name, text = payments.bank_file(db, a("Ada"), pid)
    assert name == "payment-PAY-00001.csv"
    assert (
        text.splitlines()[1]
        == "Lagos Office Supplies Ltd,0123454821,058,GTBank,5125.00,PAY-00001 Sept run"
    )
    report = payables.aging(db, as_at=date(2026, 10, 15))
    (row,) = report.rows
    # 5,375 left on 1 June's (due 1 July, 106 days) and 21,500 on 1 Sept's (due 1 Oct, 14).
    assert row.buckets == [0, D("21500.00"), 0, 0, D("5375.00")]
    listed = suppliers.list_suppliers(db, a("Chi"), today=date(2026, 10, 15))
    assert listed[0].balance == D("26875.00") and listed[0].bank == "GTBank ••••4821"
