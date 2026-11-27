"""Phase 11a: items and warehouses, adjustments posted by a second person, weighted
average and FIFO valuation, lots (earliest expiry first) and serials, transfers, counts,
material requests through the line manager, and the low-stock digest (ADR 0028)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.mail.models import MailOutbox
from app.modules.finance.models import JournalLine
from app.modules.inventory import alerts, counts, documents, items, requests, stock
from app.modules.inventory.documents import DocumentInput, LineInput
from app.modules.inventory.items import ItemInput
from app.modules.inventory.models import (
    Item,
    ItemValue,
    MaterialRequest,
    Serial,
    StockBalance,
    StockDocument,
    StockMove,
)
from tests.inventory.conftest import Stock

pytestmark = pytest.mark.db

D = Decimal
DAY = date(2026, 10, 5)


def _journal(s: Stock, journal_id: uuid.UUID | None) -> dict[str, tuple[Decimal, Decimal]]:
    assert journal_id is not None
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for jl in s.db.scalars(select(JournalLine).where(JournalLine.journal_id == journal_id)):
        debit, credit = out.get(jl.account_code, (D(0), D(0)))
        out[jl.account_code] = (debit + jl.debit, credit + jl.credit)
    return out


def _adjust(
    s: Stock, who: str, lines: list[LineInput], *, warehouse: uuid.UUID | None = None
) -> uuid.UUID:
    doc = documents.save(
        s.db,
        s.actor(who),
        None,
        DocumentInput(
            kind="adjustment",
            doc_date=DAY,
            warehouse_id=str(warehouse or s.main),
            reason="Opening stock",
            lines=tuple(lines),
        ),
    )
    documents.submit(s.db, s.actor(who), doc)
    return doc


def _post_by_other(s: Stock, doc: uuid.UUID) -> str:
    return documents.post(s.db, s.actor("Bola"), doc)


def _issue(s: Stock, item: uuid.UUID, qty: str, **line: str) -> StockDocument:
    doc = documents.save(
        s.db,
        s.actor("Hauwa"),
        None,
        DocumentInput(
            kind="issue",
            doc_date=DAY,
            warehouse_id=str(line.pop("warehouse", s.main)),
            lines=(LineInput(str(item), qty, **line),),
        ),
    )
    documents.post(s.db, s.actor("Hauwa"), doc)
    row = s.db.get(StockDocument, doc)
    assert row is not None
    return row


def _on_hand(s: Stock, item: uuid.UUID, warehouse: uuid.UUID) -> Decimal:
    return stock.on_hand(s.db, item, warehouse)


def test_catalogue_rules(stock: Stock) -> None:
    s = stock
    paper = s.item("A4 paper", reorder_level="10")
    row = s.db.get(Item, paper)
    assert row is not None and row.code == "ITM-00001"
    with pytest.raises(ValidationFailed) as exc:
        items.save_item(
            s.db,
            s.actor("Ada"),
            None,
            ItemInput(
                code="itm-00001",
                name="",
                category_id="",
                unit_id="",
                valuation="lifo",
                tracking="none",
            ),
        )
    assert {"code", "name", "category_id", "unit_id", "valuation"} <= set(
        exc.value.field_errors or {}
    )
    with pytest.raises(PermissionDenied):  # the clerk moves stock, not the catalogue
        items.save_unit(s.db, s.actor("Hauwa"), None, code="RM", name="Ream", active=True)
    with pytest.raises(ValidationFailed):
        items.save_unit(s.db, s.actor("Ada"), None, code="EA", name="Each again", active=True)
    with pytest.raises(ValidationFailed):
        items.add_bin(s.db, s.actor("Ada"), s.depot, "a1")  # codes are upper-cased

    doc = _adjust(s, "Hauwa", [LineInput(str(paper), "5", "2,000")])
    _post_by_other(s, doc)
    with pytest.raises(ValidationFailed) as exc:  # stock has moved
        items.save_item(
            s.db,
            s.actor("Ada"),
            paper,
            ItemInput(
                code="",
                name="A4 paper",
                category_id=str(row.category_id),
                unit_id=str(row.unit_id),
                valuation="fifo",
                tracking="none",
            ),
        )
    assert "valuation" in (exc.value.field_errors or {})


def test_adjustments_need_a_second_person_and_average_cost(stock: Stock) -> None:
    s = stock
    toner = s.item("Toner cartridge")
    doc = _adjust(
        s, "Ada", [LineInput(str(toner), "10", "1,000"), LineInput(str(toner), "10", "1,300")]
    )
    with pytest.raises(PermissionDenied):  # Ada recorded it
        documents.post(s.db, s.actor("Ada"), doc)
    with pytest.raises(PermissionDenied):  # the clerk can't approve
        documents.post(s.db, s.actor("Hauwa"), doc)
    assert _post_by_other(s, doc) == "SA-00001"
    row = s.db.get(StockDocument, doc)
    assert row is not None and row.value == D("23000.00")
    assert _journal(s, row.journal_id) == {"1200": (D("23000.00"), 0), "5200": (0, D("23000.00"))}

    issued = _issue(s, toner, "5")
    assert issued.number == "SI-00001" and issued.value == D("5750.00")
    assert _journal(s, issued.journal_id) == {"6300": (D("5750.00"), 0), "1200": (0, D("5750.00"))}
    with pytest.raises(Conflict, match="Only 15"):  # never negative
        _issue(s, toner, "16")
    _issue(s, toner, "15")  # the last of it takes what value is left
    worth = s.db.scalars(select(ItemValue).where(ItemValue.item_id == toner)).one()
    assert (worth.quantity, worth.value) == (0, 0)


def test_fifo_lots_earliest_expiry_first_and_transfers(stock: Stock) -> None:
    s = stock
    syrup = s.item("Cough syrup", valuation="fifo", tracking="lot")
    with pytest.raises(ValidationFailed):  # a lot item needs its lot
        _adjust(s, "Hauwa", [LineInput(str(syrup), "10", "100")])
    doc = _adjust(
        s,
        "Hauwa",
        [
            LineInput(str(syrup), "10", "100", lot_number="L-JAN", expiry=date(2027, 1, 31)),
            LineInput(str(syrup), "10", "120", lot_number="L-DEC", expiry=date(2026, 12, 31)),
        ],
    )
    _post_by_other(s, doc)
    issued = _issue(s, syrup, "12")  # all of L-DEC (expires first), then 2 of L-JAN
    assert issued.value == D("1400.00")
    moves = s.db.scalars(select(StockMove).where(StockMove.source_id == issued.id)).all()
    assert sorted(m.quantity for m in moves) == [D("-10"), D("-2")]

    transfer = documents.save(
        s.db,
        s.actor("Hauwa"),
        None,
        DocumentInput(
            kind="transfer",
            doc_date=DAY,
            warehouse_id=str(s.main),
            to_warehouse_id=str(s.depot),
            lines=(LineInput(str(syrup), "3", to_bin_id=str(s.bin_a1), lot_number="L-JAN"),),
        ),
    )
    assert documents.post(s.db, s.actor("Hauwa"), transfer) == "ST-00001"
    row = s.db.get(StockDocument, transfer)
    assert row is not None and row.journal_id is None  # same inventory account
    assert _on_hand(s, syrup, s.main) == D(5) and _on_hand(s, syrup, s.depot) == D(3)
    depot = s.db.scalars(
        select(ItemValue).where(ItemValue.item_id == syrup, ItemValue.warehouse_id == s.depot)
    ).one()
    assert depot.value == D("300.00")
    placed = s.db.scalars(
        select(StockBalance).where(StockBalance.warehouse_id == s.depot, StockBalance.quantity > 0)
    ).one()
    assert placed.bin_id == s.bin_a1 and placed.lot_id is not None


def test_serial_numbers(stock: Stock) -> None:
    s = stock
    laptop = s.item("Laptop", tracking="serial")
    with pytest.raises(ValidationFailed):
        _adjust(s, "Hauwa", [LineInput(str(laptop), "2", "500000", serials="SN1")])
    doc = _adjust(s, "Hauwa", [LineInput(str(laptop), "2", "500,000", serials="SN1, SN2")])
    _post_by_other(s, doc)
    _issue(s, laptop, "1", serials="SN1")
    with pytest.raises(Conflict, match="SN1 isn't in stock"):
        _issue(s, laptop, "1", serials="SN1")
    gone = s.db.scalars(select(Serial).where(Serial.number == "SN1")).one()
    assert not gone.in_stock and gone.warehouse_id is None
    again = _adjust(s, "Hauwa", [LineInput(str(laptop), "1", "500000", serials="SN2")])
    with pytest.raises(Conflict, match="already in stock"):
        _post_by_other(s, again)


def test_counts_post_variances_through_a_second_person(stock: Stock) -> None:
    s = stock
    pens = s.item("Pens")
    _post_by_other(s, _adjust(s, "Hauwa", [LineInput(str(pens), "100", "50")]))
    count = counts.start(s.db, s.actor("Hauwa"), warehouse_id=str(s.main), on=DAY, note="")
    view = counts.get(s.db, s.actor("Hauwa"), count)
    assert [(x.code, x.expected) for x in view.lines] == [("ITM-00001", D(100))]
    with pytest.raises(Conflict):  # every line needs a count
        counts.submit(s.db, s.actor("Hauwa"), count)
    counts.save_counts(s.db, s.actor("Hauwa"), count, {str(view.lines[0].id): "96"})
    counts.submit(s.db, s.actor("Hauwa"), count)
    with pytest.raises(PermissionDenied):
        counts.post(s.db, s.actor("Hauwa"), count)
    counts.post(s.db, s.actor("Bola"), count)
    view = counts.get(s.db, s.actor("Bola"), count)
    assert view.count.value == D("-200.00") and view.lines[0].value == D("-200.00")
    assert _journal(s, view.count.journal_id) == {
        "5200": (D("200.00"), 0),
        "1200": (0, D("200.00")),
    }
    assert _on_hand(s, pens, s.main) == D(96)


def test_material_requests_go_to_the_line_manager(stock: Stock) -> None:
    s = stock
    gloves = s.item("Gloves")
    _post_by_other(s, _adjust(s, "Hauwa", [LineInput(str(gloves), "10", "200")]))
    rid = requests.save(
        s.db,
        s.actor("Dayo"),
        None,
        requests.RequestInput(str(s.main), None, "Office cleaning", ((str(gloves), "4"),)),
    )
    assert requests.submit(s.db, s.actor("Dayo"), rid) == "MR-00001"
    row = s.db.get(MaterialRequest, rid)
    assert row is not None and row.approver_employee_id == s.hr.employee("Chi")
    with pytest.raises(PermissionDenied):  # nobody decides their own
        requests.decide(s.db, s.actor("Dayo"), rid, approve=True, note="")
    with pytest.raises(PermissionDenied):  # Femi isn't Dayo's manager
        requests.decide(s.db, s.actor("Femi"), rid, approve=True, note="")
    assert [r.id for r in requests.list_requests(s.db, s.actor("Chi"), "approve")] == [rid]
    with pytest.raises(ValidationFailed):  # a rejection needs a reason
        requests.decide(s.db, s.actor("Chi"), rid, approve=False, note="")
    requests.decide(s.db, s.actor("Chi"), rid, approve=True, note="Fine")
    line = requests.get(s.db, s.actor("Hauwa"), rid).lines[0]
    with pytest.raises(ValidationFailed):
        requests.issue(
            s.db, s.actor("Hauwa"), rid, quantities={str(line.id): "5"}, account_id="", on=DAY
        )
    number = requests.issue(
        s.db, s.actor("Hauwa"), rid, quantities={str(line.id): "3"}, account_id="", on=DAY
    )
    assert number == "SI-00001"
    assert requests.get(s.db, s.actor("Dayo"), rid).status == "partly"
    requests.issue(
        s.db, s.actor("Hauwa"), rid, quantities={str(line.id): "1"}, account_id="", on=DAY
    )
    view = requests.get(s.db, s.actor("Dayo"), rid)
    assert view.status == "issued" and [n for _, n in view.issues] == ["SI-00001", "SI-00002"]
    issue = s.db.scalars(select(StockDocument).where(StockDocument.number == "SI-00001")).one()
    assert issue.request_id == rid and issue.cost_centre_id == row.cost_centre_id


def test_low_stock_digest_once_a_day(stock: Stock) -> None:
    s = stock
    tape = s.item("Packing tape", reorder_level="10", reorder_quantity="50")
    _post_by_other(s, _adjust(s, "Hauwa", [LineInput(str(tape), "8", "300")]))
    assert {(i, w) for i, w in items.low_places(s.db)} == {(tape, s.main)}
    sent = alerts.send_digest(s.db, date(2026, 11, 26), "Adeyemi Holdings")
    assert sent == 2  # Ada and Bola manage items
    mail = s.db.scalars(
        select(MailOutbox).where(MailOutbox.template == "inventory_low_stock")
    ).all()
    assert len(mail) == 2 and "Packing tape" in mail[0].body_text
    assert alerts.send_digest(s.db, date(2026, 11, 26), "Adeyemi Holdings") == 0
    assert alerts.send_digest(s.db, date(2026, 11, 27), "Adeyemi Holdings") == 2
