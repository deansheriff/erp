"""Phase 11c: requisitions approved through tiers by amount (one step each, never your
own, the Director stepping in without a line manager), RFQs emailed with a PDF, quotes,
the three-quote rule, awards with a reason, re-approval over the tier, the draft purchase
order, and supplier performance with ratings (ADR 0030)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import clock
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.models import User
from app.mail.models import MailOutbox
from app.modules.finance import bills, purchasing, sales
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.models import PurchaseLine, PurchaseOrder
from app.modules.procurement import performance, requisitions, rfqs
from app.modules.procurement import settings as setup
from app.modules.procurement.models import Requisition
from app.modules.procurement.requisitions import LineInput, RequisitionInput
from tests.procurement.conftest import Buying

pytestmark = pytest.mark.db

D = Decimal


def _raise(b: Buying, who: str, lines: list[LineInput], purpose: str = "Q4 supplies") -> uuid.UUID:
    data = RequisitionInput(None, purpose, tuple(lines))
    row = requisitions.save(b.db, b.actor(who), None, data)
    requisitions.submit(b.db, b.actor(who), row)
    return row


def _free(b: Buying, what: str, qty: str, price: str, account: str = "") -> LineInput:
    acct = str(b.account(account).id) if account else ""
    return LineInput("", "", what, acct, qty, price)


def _rice(b: Buying, qty: str, price: str) -> LineInput:
    return LineInput(str(b.rice), str(b.main), "", "", qty, price)


def _levels(b: Buying, row: uuid.UUID) -> list[tuple[str, str]]:
    return [(s.level, s.status) for s in requisitions.steps_of(b.db, row)]


def _status(b: Buying, row: uuid.UUID) -> str:
    found = b.db.get(Requisition, row)
    assert found is not None
    return found.status


# ------------------------------------------------------------------ requisitions


def test_tiers_are_decided_in_order_by_different_people(buying: Buying) -> None:
    b, a = buying, buying.actor
    row = _raise(
        b, "Dayo", [_free(b, "Printer servicing", "1", "300,000"), _rice(b, "10", "62,000")]
    )
    req = b.db.get(Requisition, row)
    assert req is not None and req.number == "PR-00001"
    assert req.estimated_total == D("920000.00")
    assert _levels(b, row) == [("line_manager", "pending"), ("finance", "pending")]
    with pytest.raises(PermissionDenied):
        requisitions.decide(b.db, a("Bola"), row, approve=True, note="")  # Chi's step first
    with pytest.raises(PermissionDenied):
        requisitions.decide(b.db, a("Dayo"), row, approve=True, note="")  # never your own
    assert requisitions.decide(b.db, a("Chi"), row, approve=True, note="Fine") == "submitted"
    with pytest.raises(PermissionDenied):
        requisitions.decide(b.db, a("Chi"), row, approve=True, note="")  # not Finance
    assert requisitions.decide(b.db, a("Bola"), row, approve=True, note="") == "approved"

    # Someone holding both approvals still signs only one step.
    chi = b.db.get(User, b.hr.org.users["Chi"])
    assert chi is not None
    b.hr.people.give(chi, "finance_manager")
    second = _raise(b, "Dayo", [_free(b, "Desks", "4", "150,000", "6300")])
    requisitions.decide(b.db, a("Chi"), second, approve=True, note="")
    assert [r.id for r in requisitions.list_requisitions(b.db, a("Chi"), "approve")] == []
    with pytest.raises(PermissionDenied):
        requisitions.decide(b.db, a("Chi"), second, approve=True, note="")
    with pytest.raises(ValidationFailed):
        requisitions.decide(b.db, a("Bola"), second, approve=False, note="")
    assert requisitions.decide(b.db, a("Bola"), second, approve=False, note="No budget") == (
        "rejected"
    )


def test_the_director_steps_in_without_a_line_manager(buying: Buying) -> None:
    b, a = buying, buying.actor
    small = _raise(b, "Hauwa", [_free(b, "Toner", "2", "45,000")])
    assert _levels(b, small) == [("line_manager", "pending")]
    with pytest.raises(PermissionDenied):
        requisitions.decide(b.db, a("Chi"), small, approve=True, note="")
    assert requisitions.decide(b.db, a("Ada"), small, approve=True, note="") == "approved"

    big = _raise(b, "Dayo", [_free(b, "Generator", "1", "6,500,000")])
    assert [level for level, _ in _levels(b, big)] == ["line_manager", "finance", "director"]


def test_drafts_are_checked_and_only_the_owner_changes_them(buying: Buying) -> None:
    b, a = buying, buying.actor
    with pytest.raises(ValidationFailed) as bad:
        requisitions.save(
            b.db,
            a("Dayo"),
            None,
            RequisitionInput(
                None, "", (_free(b, "Chairs", "0", "10,000"), _free(b, "Pens", "1", ""))
            ),
        )
    errors = bad.value.field_errors or {}
    assert set(errors) == {"purpose", "line_0", "line_1"}
    draft = requisitions.save(
        b.db, a("Dayo"), None, RequisitionInput(None, "Pens", (_free(b, "Pens", "5", "500"),))
    )
    with pytest.raises(Conflict):
        requisitions.save(
            b.db,
            a("Hauwa"),
            draft,
            RequisitionInput(None, "Mine", (_free(b, "Pens", "5", "500"),)),
        )
    requisitions.cancel(b.db, a("Dayo"), draft)
    assert b.db.get(Requisition, draft) is None


# ------------------------------------------------------------------ RFQs and awards


def _quote(
    b: Buying, rfq: uuid.UUID, supplier: str, prices: list[str], days: str = "7"
) -> uuid.UUID:
    lines = requisitions.lines_of(b.db, rfqs._rfq(b.db, rfq).requisition_id)
    data = rfqs.QuoteInput(
        f"Q-{supplier[:3]}",
        clock.today(),
        clock.today() + timedelta(days=30),
        days,
        "",
        {str(line.id): price for line, price in zip(lines, prices, strict=True)},
        {str(line.id): "standard" for line in lines},
    )
    return rfqs.record_quote(b.db, b.actor("Gbenga"), rfq, b.suppliers[supplier], data)


def _approved(b: Buying, lines: list[LineInput]) -> uuid.UUID:
    row = _raise(b, "Dayo", lines)
    for who in ("Chi", "Bola", "Ada"):
        step = requisitions.current_step(b.db, row)
        if step is None:
            break
        requisitions.decide(b.db, b.actor(who), row, approve=True, note="")
    assert _status(b, row) == "approved"
    return row


def test_rfq_quotes_award_and_the_draft_purchase_order(buying: Buying) -> None:
    b, a = buying, buying.actor
    row = _approved(b, [_rice(b, "20", "60,000"), _free(b, "Delivery", "1", "50,000")])
    with pytest.raises(PermissionDenied):
        rfqs.start(b.db, a("Dayo"), row, respond_by=None, note="", supplier_ids=[])
    rfq = rfqs.start(
        b.db,
        a("Gbenga"),
        row,
        respond_by=clock.today() + timedelta(days=5),
        note="Deliver to the Ikeja store.",
        supplier_ids=[str(s) for s in b.suppliers.values()],
    )
    sent = rfqs.send(b.db, a("Gbenga"), rfq)
    assert sent.emailed == 1
    assert sorted(sent.without_email) == ["Abuja Traders", "Lagos Office Supplies Ltd"]
    outbox = b.db.scalars(select(MailOutbox).where(MailOutbox.template == "procurement_rfq")).one()
    assert outbox.attachment_name == "RFQ-00001.pdf"
    assert outbox.attachment_data and outbox.attachment_data.startswith(b"%PDF")

    # 1,250,000 is over the 1,000,000 threshold: three quotes before an award.
    kano = _quote(b, rfq, "Kano Grains Ltd", ["61,000", "40,000"], days="5")
    lagos = _quote(b, rfq, "Lagos Office Supplies Ltd", ["58,000", "45,000"])
    with pytest.raises(Conflict, match="at least 3 quotes"):
        rfqs.award(b.db, a("Gbenga"), rfq, str(kano), reason="")
    _quote(b, rfq, "Abuja Traders", ["63,000", "30,000"])
    view = rfqs.get(b.db, a("Gbenga"), rfq)
    assert [q.id for q in view.quotes if q.lowest] == [lagos]  # 1,205,000 net
    with pytest.raises(ValidationFailed):
        rfqs.award(b.db, a("Gbenga"), rfq, str(kano), reason="")  # not the lowest
    assert rfqs.award(b.db, a("Gbenga"), rfq, str(kano), reason="Delivers in 5 days") == ("awarded")

    lines = requisitions.lines_of(b.db, row)
    with pytest.raises(ValidationFailed) as no_account:
        rfqs.make_order(b.db, a("Gbenga"), rfq, accounts={})
    assert set(no_account.value.field_errors or {}) == {f"account_{lines[1].id}"}
    order_id = rfqs.make_order(
        b.db, a("Gbenga"), rfq, accounts={str(lines[1].id): str(b.account("6300").id)}
    )
    order = b.db.get(PurchaseOrder, order_id)
    assert order is not None and order.status == "draft"
    assert order.supplier_id == b.suppliers["Kano Grains Ltd"]
    assert order.expected_date == clock.today() + timedelta(days=5)
    po_lines = purchasing.order_lines(b.db, order_id)
    assert [(x.item_id, x.warehouse_id, x.unit_price) for x in po_lines] == [
        (b.rice, b.main, D("61000.00")),
        (None, None, D("40000.00")),
    ]
    assert po_lines[1].account_id == b.account("6300").id
    assert _status(b, row) == "ordered"
    with pytest.raises(PermissionDenied):
        purchasing.approve_order(b.db, a("Gbenga"), order_id)  # someone else approves
    assert purchasing.approve_order(b.db, a("Bola"), order_id).startswith("PO-")


def test_an_award_over_the_approved_tier_goes_back_for_approval(buying: Buying) -> None:
    b, a = buying, buying.actor
    row = _approved(b, [_free(b, "Office chairs", "10", "60,000", "6300")])  # 600,000
    assert [level for level, _ in _levels(b, row)] == ["line_manager", "finance"]
    rfq = rfqs.start(
        b.db,
        a("Gbenga"),
        row,
        respond_by=None,
        note="",
        supplier_ids=[str(b.suppliers["Abuja Traders"])],
    )
    quote = _quote(b, rfq, "Abuja Traders", ["600,000"])  # 6,000,000: the Director's tier
    assert rfqs.award(b.db, a("Gbenga"), rfq, str(quote), reason="") == "approval"
    assert _status(b, row) == "submitted"
    assert rfqs.get(b.db, a("Gbenga"), rfq).waiting_for == ["Director"]
    with pytest.raises(Conflict):
        rfqs.make_order(b.db, a("Gbenga"), rfq, accounts={})
    requisitions.decide(b.db, a("Ada"), row, approve=True, note="Approved for the quote")
    assert _status(b, row) == "approved"
    assert rfqs.make_order(b.db, a("Gbenga"), rfq, accounts={})


def test_settings_change_the_tiers_and_threshold(buying: Buying) -> None:
    b, a = buying, buying.actor
    data = setup.SettingsInput(
        "2,000,000",
        {"finance": "800,000", "director": "700,000"},
        {"finance": True, "director": True},
    )
    with pytest.raises(PermissionDenied):
        setup.save(b.db, a("Gbenga"), data)
    with pytest.raises(ValidationFailed):
        setup.save(b.db, a("Bola"), data)  # the Director's amount must be higher
    setup.save(
        b.db,
        a("Bola"),
        setup.SettingsInput(
            "2,000,000", {"finance": "800,000", "director": "9,000,000"}, {"finance": True}
        ),
    )
    assert setup.levels_for(b.db, D("850000")) == ["line_manager", "finance"]
    assert setup.levels_for(b.db, D("20000000")) == ["line_manager", "finance"]  # off
    assert setup.quotes_needed(b.db, D("1500000")) == 1


# ------------------------------------------------------------------ performance


def test_supplier_performance_and_ratings(buying: Buying) -> None:
    b, a = buying, buying.actor
    kano = b.suppliers["Kano Grains Ltd"]
    lines = [
        sales.LineInput("Paper", str(b.account("6300").id), "", "10", "5,000", "standard"),
        sales.LineInput("Toner", str(b.account("6300").id), "", "5", "20,000", "standard"),
    ]
    order = purchasing.save_order(
        b.db,
        a("Gbenga"),
        None,
        purchasing.OrderInput(str(kano), date(2026, 10, 1), date(2026, 10, 10), "", "", lines),
    )
    purchasing.approve_order(b.db, a("Bola"), order)
    po = purchasing.order_lines(b.db, order)
    for line, day in zip(po, (date(2026, 10, 8), date(2026, 10, 12)), strict=True):
        purchasing.receive(
            b.db,
            a("Gbenga"),
            order,
            received_on=day,
            delivery_note="",
            note="",
            quantities={str(line.id): f"{line.quantity}"},
        )
    with pytest.raises(PermissionDenied):
        performance.rate(b.db, a("Dayo"), kano, order, score="4", comment="")
    bill = bills.bill_from_order(b.db, a("Gbenga"), order, today=date(2026, 10, 15))
    view = bills.get_bill(b.db, a("Gbenga"), bill, today=date(2026, 10, 15))
    bills.save_bill(
        b.db,
        a("Gbenga"),
        bill,
        BillInput(
            "bill",
            str(kano),
            "KG-881",
            date(2026, 10, 15),
            None,
            "",
            "",
            [
                BillLineInput(
                    str(x.order_line_id),
                    x.description,
                    str(x.account_id),
                    "",
                    f"{x.quantity}",
                    f"{x.unit_price}",
                    x.vat_code,
                )
                for x in view.lines
            ],
        ),
    )
    bills.approve(b.db, a("Bola"), bill)
    performance.rate(b.db, a("Gbenga"), kano, order, score="4", comment="Toner came late")
    with pytest.raises(ValidationFailed):
        performance.rate(b.db, a("Gbenga"), kano, order, score="9", comment="")

    card = next(c for c in performance.scorecards(b.db, a("Gbenga")) if c.supplier_id == kano)
    assert (card.orders, card.deliveries, card.on_time, card.on_time_pct) == (1, 2, 1, 50)
    assert (card.in_full_pct, card.returned_pct, card.tolerance_pct) == (100, D("0.0"), 100)
    assert (card.rating, card.ratings) == (D("4.0"), 1)
    detail = performance.detail(b.db, a("Gbenga"), kano)
    assert detail.orders[0].facts.days_late == 2
    assert detail.orders[0].rating is not None and detail.orders[0].rating.score == 4
    assert b.db.scalars(select(PurchaseLine).where(PurchaseLine.order_id == order)).first()
