"""Phase 11b: stock in purchasing and sales (ADR 0029). Goods received bring stock in at the
order price against goods received not invoiced; the bill clears it with any price
difference to the variance account; debit notes send stock back at cost; invoices take
stock out at cost into cost of sales, and credit notes put it back."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, ValidationFailed
from app.identity.models import User
from app.modules.finance import bills, customers, purchasing, sales, suppliers
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.customers import CustomerInput
from app.modules.finance.models import (
    Bill,
    GoodsReceipt,
    JournalLine,
    PurchaseLine,
    SalesDocument,
    Supplier,
    WhtType,
)
from app.modules.finance.suppliers import SupplierInput
from app.modules.inventory import documents, stock
from app.modules.inventory.documents import DocumentInput
from app.modules.inventory.documents import LineInput as StockLineInput
from app.modules.inventory.models import StockMove
from tests.inventory.conftest import Stock

pytestmark = pytest.mark.db

D = Decimal
ORDERED = date(2026, 10, 1)
ARRIVED = date(2026, 10, 3)


@pytest.fixture()
def s(stock: Stock) -> Stock:
    """Bola is a Finance Manager too, to approve what Ada drafts."""
    user = stock.db.get(User, stock.hr.org.users["Bola"])
    assert user is not None
    stock.hr.people.give(user, "finance_manager")
    return stock


def _journal(s: Stock, journal_id: uuid.UUID | None) -> dict[str, tuple[Decimal, Decimal]]:
    assert journal_id is not None
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for jl in s.db.scalars(select(JournalLine).where(JournalLine.journal_id == journal_id)):
        debit, credit = out.get(jl.account_code, (D(0), D(0)))
        out[jl.account_code] = (debit + jl.debit, credit + jl.credit)
    return out


def _supplier(s: Stock) -> uuid.UUID:
    known = s.db.scalars(select(Supplier.id).where(Supplier.name == "Kano Grains Ltd")).first()
    if known:
        return known
    wht = s.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    return suppliers.save(
        s.db,
        s.actor("Ada"),
        None,
        SupplierInput(
            "Kano Grains Ltd",
            "company",
            "sales@kanograins.example",
            "",
            "",
            "",
            "30",
            str(wht.id),
            "",
            "",
            "",
            "",
            True,
        ),
    )


def _order(s: Stock, lines: list[sales.LineInput]) -> uuid.UUID:
    """Ada drafts, Bola approves."""
    data = purchasing.OrderInput(str(_supplier(s)), ORDERED, None, "", "", lines)
    order = purchasing.save_order(s.db, s.actor("Ada"), None, data)
    purchasing.approve_order(s.db, s.actor("Bola"), order)
    return order


def _stock_line(
    item: uuid.UUID, warehouse: uuid.UUID, qty: str, price: str, vat: str = "exempt"
) -> sales.LineInput:
    return sales.LineInput(
        "", "", "", qty, price, vat, item_id=str(item), warehouse_id=str(warehouse)
    )


def _receive(s: Stock, order: uuid.UUID, qty: str, **detail: str) -> GoodsReceipt:
    line = purchasing.order_lines(s.db, order)[0]
    expiry = detail.pop("expiry", "")
    number = purchasing.receive(
        s.db,
        s.actor("Ada"),
        order,
        received_on=ARRIVED,
        delivery_note="DN-1",
        note="",
        quantities={str(line.id): qty},
        details={
            str(line.id): purchasing.ReceiveDetail(
                expiry=date.fromisoformat(expiry) if expiry else None, **detail
            )
        },
    )
    return s.db.scalars(select(GoodsReceipt).where(GoodsReceipt.number == number)).one()


def _bill_lines(s: Stock, bill_id: uuid.UUID, qty: str, price: str, serials: str = "") -> None:
    view = bills.get_bill(s.db, s.actor("Ada"), bill_id, today=ARRIVED)
    data = BillInput(
        view.kind,
        str(view.supplier_id),
        "KG-2231" if view.kind == "bill" else "",
        ARRIVED,
        None,
        "",
        "",
        [
            BillLineInput(
                str(line.order_line_id or ""),
                line.description,
                str(line.account_id),
                "",
                qty,
                price,
                line.vat_code,
                serials,
            )
            for line in view.lines
        ],
    )
    bills.save_bill(s.db, s.actor("Ada"), bill_id, data)


def _journal_of(s: Stock, bill_id: uuid.UUID) -> dict[str, tuple[Decimal, Decimal]]:
    row = s.db.get(Bill, bill_id)
    assert row is not None
    return _journal(s, row.journal_id)


# ------------------------------------------------------------------ purchasing


def test_goods_received_come_into_stock_and_the_bill_clears_grni(s: Stock) -> None:
    rice = s.item("Rice 50kg")
    order = _order(s, [_stock_line(rice, s.main, "10", "20,000")])
    po_line = purchasing.order_lines(s.db, order)[0]
    assert po_line.description == "Rice 50kg"  # defaults to the item's name
    assert po_line.account_id == s.account("1200").id  # charged to inventory

    grn = _receive(s, order, "6")
    assert stock.on_hand(s.db, rice, s.main) == D(6)
    assert _journal(s, grn.journal_id) == {
        "1200": (D("120000.00"), D(0)),
        "2030": (D(0), D("120000.00")),
    }
    moves = list(s.db.scalars(select(StockMove).where(StockMove.source == "goods_receipt")))
    assert [(m.kind, m.number, m.journal_id) for m in moves] == [
        ("receipt", grn.number, grn.journal_id)
    ]

    bill = bills.bill_from_order(s.db, s.actor("Ada"), order, today=ARRIVED)
    _bill_lines(s, bill, "6", "20,200")  # 1% over the order: within the tolerance
    assert bills.approve(s.db, s.actor("Bola"), bill) == "posted"
    assert _journal_of(s, bill) == {
        "2030": (D("120000.00"), D(0)),  # cleared at the order price
        "5200": (D("1200.00"), D(0)),  # the price difference
        "2010": (D(0), D("121200.00")),
    }
    assert stock.on_hand(s.db, rice, s.main) == D(6)  # billing moves no stock

    note = bills.debit_note_for(s.db, s.actor("Ada"), bill, today=ARRIVED)
    _bill_lines(s, note, "2", "20,200")
    assert bills.approve(s.db, s.actor("Bola"), note) == "posted"
    assert stock.on_hand(s.db, rice, s.main) == D(4)
    assert _journal_of(s, note) == {
        "2010": (D("40400.00"), D(0)),
        "1200": (D(0), D("40000.00")),  # at cost
        "5200": (D(0), D("400.00")),
    }
    returned = s.db.scalars(select(StockMove).where(StockMove.kind == "purchase_return")).one()
    assert returned.journal_id == s.db.get(Bill, note).journal_id  # type: ignore[union-attr]
    done = purchasing.progress(s.db, order)[po_line.id]
    assert (done.received, done.billed, done.to_receive) == (D(4), D(4), D(6))


def test_receiving_lots_serials_and_bins_and_lines_that_arent_stock(s: Stock) -> None:
    flour = s.item("Flour 25kg", valuation="fifo", tracking="lot")
    order = _order(s, [_stock_line(flour, s.depot, "5", "1,000")])
    line = purchasing.order_lines(s.db, order)[0]
    with pytest.raises(ValidationFailed) as no_lot, s.db.begin_nested():
        _receive(s, order, "5")
    assert f"qty_{line.id}" in (no_lot.value.field_errors or {})
    _receive(s, order, "5", lot_number="L-0926", expiry="2027-03-31", bin_id=str(s.bin_a1))
    assert stock.on_hand(s.db, flour, s.depot) == D(5)
    move = s.db.scalars(select(StockMove).where(StockMove.item_id == flour)).one()
    assert move.bin_id == s.bin_a1 and move.lot_id is not None

    laptop = s.item("Laptop", tracking="serial")
    order = _order(s, [_stock_line(laptop, s.main, "2", "500,000")])
    with pytest.raises(ValidationFailed), s.db.begin_nested():
        _receive(s, order, "2", serials="SN-1")
    _receive(s, order, "2", serials="SN-1, SN-2")
    assert stock.on_hand(s.db, laptop, s.main) == D(2)

    paper = sales.LineInput("A4 paper", str(s.account("5200").id), "", "1", "5,000", "exempt")
    plain = _receive(s, _order(s, [paper]), "1")
    assert plain.journal_id is None  # nothing to stock, nothing posted

    bad = sales.LineInput("", "", "", "1", "100", "exempt", item_id=str(laptop))
    with pytest.raises(ValidationFailed) as no_store:
        purchasing.save_order(
            s.db,
            s.actor("Ada"),
            None,
            purchasing.OrderInput(str(_supplier(s)), ORDERED, None, "", "", [bad]),
        )
    assert "warehouse" in (no_store.value.field_errors or {})["line_0"]


def test_returning_more_serials_than_are_in_stock_is_refused(s: Stock) -> None:
    laptop = s.item("Laptop", tracking="serial")
    order = _order(s, [_stock_line(laptop, s.main, "2", "500,000")])
    _receive(s, order, "2", serials="SN-1, SN-2")
    bill = bills.bill_from_order(s.db, s.actor("Ada"), order, today=ARRIVED)
    _bill_lines(s, bill, "2", "500,000")
    bills.approve(s.db, s.actor("Bola"), bill)
    note = bills.debit_note_for(s.db, s.actor("Ada"), bill, today=ARRIVED)
    _bill_lines(s, note, "1", "500,000", serials="SN-9")
    with pytest.raises(Conflict, match="SN-9"), s.db.begin_nested():
        bills.approve(s.db, s.actor("Bola"), note)
    _bill_lines(s, note, "1", "500,000", serials="SN-2")
    assert bills.approve(s.db, s.actor("Bola"), note) == "posted"
    assert stock.on_hand(s.db, laptop, s.main) == D(1)


# ------------------------------------------------------------------ sales


def _opening(s: Stock, item: uuid.UUID, qty: str, cost: str) -> None:
    doc = documents.save(
        s.db,
        s.actor("Hauwa"),
        None,
        DocumentInput(
            kind="adjustment",
            doc_date=date(2026, 10, 1),
            warehouse_id=str(s.main),
            reason="Opening stock",
            lines=(StockLineInput(str(item), qty, cost),),
        ),
    )
    documents.submit(s.db, s.actor("Hauwa"), doc)
    documents.post(s.db, s.actor("Bola"), doc)


def _customer(s: Stock) -> uuid.UUID:
    return customers.save(
        s.db,
        s.actor("Ada"),
        None,
        CustomerInput(
            "Adebayo Foods Ltd", "accounts@adebayo.example", "", "", "", "30", True, True
        ),
    )


def _sale_line(s: Stock, item: uuid.UUID, qty: str, price: str) -> sales.LineInput:
    return sales.LineInput(
        "",
        str(s.account("4100").id),
        "",
        qty,
        price,
        "exempt",
        item_id=str(item),
        warehouse_id=str(s.main),
    )


def _draft(
    s: Stock,
    kind: str,
    customer: uuid.UUID,
    lines: list[sales.LineInput],
    document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    data = sales.DocumentInput(
        kind, str(customer), date(2026, 10, 6), None, None, "", "", "", lines
    )
    return sales.save_draft(s.db, s.actor("Ada"), document_id, data)


def test_an_invoice_takes_stock_out_at_cost_and_a_credit_note_puts_it_back(s: Stock) -> None:
    beans = s.item("Beans 50kg")
    _opening(s, beans, "10", "5,000")
    who = _customer(s)

    too_many = _draft(s, "invoice", who, [_sale_line(s, beans, "11", "8,000")])
    with pytest.raises(Conflict, match="Beans 50kg"), s.db.begin_nested():
        sales.issue(s.db, s.actor("Ada"), too_many)  # never negative stock

    invoice = _draft(s, "invoice", who, [_sale_line(s, beans, "4", "8,000")])
    sales.issue(s.db, s.actor("Ada"), invoice)
    assert stock.on_hand(s.db, beans, s.main) == D(6)
    row = s.db.get(SalesDocument, invoice)
    assert row is not None
    assert _journal(s, row.journal_id) == {
        "1100": (D("32000.00"), D(0)),
        "4100": (D(0), D("32000.00")),
        "5100": (D("20000.00"), D(0)),  # cost of sales at the average cost
        "1200": (D(0), D("20000.00")),
    }
    sale = s.db.scalars(select(StockMove).where(StockMove.kind == "sale")).one()
    assert (sale.number, sale.journal_id) == (row.number, row.journal_id)

    _opening(s, beans, "6", "6,000")  # the average cost is now 5,500
    note = sales.credit_note_for(s.db, s.actor("Ada"), invoice)
    _draft(s, "credit_note", who, [_sale_line(s, beans, "1", "8,000")], document_id=note)
    sales.issue(s.db, s.actor("Bola"), note)
    assert stock.on_hand(s.db, beans, s.main) == D(13)
    credit = s.db.get(SalesDocument, note)
    assert credit is not None
    journal = _journal(s, credit.journal_id)
    assert journal["1200"] == (D("5000.00"), D(0))  # back at what the invoice took out
    assert journal["5100"] == (D(0), D("5000.00"))


def test_an_invoice_without_stock_lines_moves_nothing(s: Stock) -> None:
    who = _customer(s)
    service = sales.LineInput("Delivery", str(s.account("4100").id), "", "1", "2,000", "exempt")
    invoice = _draft(s, "invoice", who, [service])
    sales.issue(s.db, s.actor("Ada"), invoice)
    assert s.db.scalars(select(StockMove)).first() is None
    assert s.db.scalars(select(PurchaseLine)).first() is None
