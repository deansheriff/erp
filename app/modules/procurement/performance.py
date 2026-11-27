"""Supplier performance (ADR 0030), shown as separate measures with no combined score:
on-time delivery from goods received against the PO's expected date; quantities received
in full and stock sent back; bills within the price tolerance and how often quotes win;
and procurement's 1-5 rating of each order once it's received."""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.procurement import rfqs
from app.modules.procurement import settings as setup
from app.modules.procurement.models import Quote, SupplierRating

READ = rfqs.READ
RATE = "procurement.supplier.rate"
ZERO = Decimal(0)
RATEABLE = ("received", "closed")
SINCE_EVER = date(2000, 1, 1)


def _pct(part: int | Decimal, whole: int | Decimal) -> int | None:
    return round(Decimal(part) * 100 / Decimal(whole)) if whole else None


@dataclass(frozen=True, slots=True)
class Scorecard:
    supplier_id: uuid.UUID
    name: str
    orders: int
    deliveries: int  # goods received notes on orders with an expected date
    on_time: int
    lines: int
    lines_in_full: int
    received: Decimal
    returned: Decimal
    bill_lines: int
    in_tolerance: int
    quotes: int
    wins: int
    rating: Decimal | None
    ratings: int

    @property
    def on_time_pct(self) -> int | None:
        return _pct(self.on_time, self.deliveries)

    @property
    def in_full_pct(self) -> int | None:
        return _pct(self.lines_in_full, self.lines)

    @property
    def returned_pct(self) -> Decimal | None:
        if not self.received:
            return None
        return (self.returned * 100 / self.received).quantize(Decimal("0.1"))

    @property
    def tolerance_pct(self) -> int | None:
        return _pct(self.in_tolerance, self.bill_lines)


def _ratings(db: Session, supplier_id: uuid.UUID) -> dict[uuid.UUID, SupplierRating]:
    return {
        r.order_id: r
        for r in db.scalars(select(SupplierRating).where(SupplierRating.supplier_id == supplier_id))
    }


def _scorecard(
    db: Session,
    supplier: finance.SupplierInfo,
    facts: list[finance.OrderFacts],
    since: date,
) -> Scorecard:
    quotes = [q for _, q in rfqs.supplier_quotes(db, supplier.id) if q.received_on >= since]
    ratings = [r for r in _ratings(db, supplier.id).values() if r.rated_at.date() >= since]
    return Scorecard(
        supplier.id,
        supplier.name,
        len(facts),
        sum(len(f.receipts) for f in facts if f.expected_date),
        sum(f.on_time for f in facts),
        sum(f.lines for f in facts),
        sum(f.lines_in_full for f in facts),
        sum((f.received for f in facts), ZERO),
        sum((f.returned for f in facts), ZERO),
        sum(f.bill_lines for f in facts),
        sum(f.bill_lines_in_tolerance for f in facts),
        len(quotes),
        sum(1 for q in quotes if q.awarded),
        (Decimal(sum(r.score for r in ratings)) / len(ratings)).quantize(Decimal("0.1"))
        if ratings
        else None,
        len(ratings),
    )


def since_for(months: int) -> date:
    return clock.today() - timedelta(days=round(months * 365 / 12))


def scorecards(db: Session, actor: Actor, *, months: int = 12) -> list[Scorecard]:
    """Suppliers with orders or quotes in the period, busiest first."""
    setup.require(db, actor, READ)
    since = since_for(months)
    counts = finance.orders_by_supplier(db, since=since)
    quoted = set(db.scalars(select(Quote.supplier_id).where(Quote.received_on >= since).distinct()))
    wanted = set(counts) | quoted
    out = [
        _scorecard(db, s, finance.supplier_history(db, s.id, since=since), since)
        for s in finance.all_suppliers(db)
        if s.id in wanted
    ]
    return sorted(out, key=lambda c: (-c.orders, c.name))


@dataclass(frozen=True, slots=True)
class OrderRow:
    facts: finance.OrderFacts
    rating: SupplierRating | None
    rated_by: str | None

    @property
    def rateable(self) -> bool:
        return self.facts.status in RATEABLE


@dataclass(frozen=True, slots=True)
class QuoteRow:
    rfq_id: uuid.UUID
    rfq: str
    received_on: date
    net: Decimal
    result: str  # won, lost, open


@dataclass(frozen=True, slots=True)
class SupplierPerformance:
    supplier: finance.SupplierInfo
    card: Scorecard
    orders: list[OrderRow] = field(default_factory=list)
    quotes: list[QuoteRow] = field(default_factory=list)
    can_rate: bool = False


def _rater(names: dict[uuid.UUID, str], rating: SupplierRating | None) -> str | None:
    return names.get(rating.rated_by) if rating and rating.rated_by else None


def detail(
    db: Session, actor: Actor, supplier_id: uuid.UUID, *, months: int = 12
) -> SupplierPerformance:
    setup.require(db, actor, READ)
    supplier = finance.suppliers_by_id(db, {supplier_id}).get(supplier_id)
    if supplier is None:
        raise NotFound()
    since = since_for(months)
    facts = finance.supplier_history(db, supplier_id, since=since)
    ratings = _ratings(db, supplier_id)
    raters = setup.user_names(db, {r.rated_by for r in ratings.values()})
    quotes = []
    for rfq, quote in rfqs.supplier_quotes(db, supplier_id):
        if quote.received_on < since:
            continue
        decided = rfq.status in ("awarded", "ordered")
        result = "won" if quote.awarded else ("lost" if decided else "open")
        quotes.append(QuoteRow(rfq.id, rfq.number, quote.received_on, quote.net, result))
    return SupplierPerformance(
        supplier,
        _scorecard(db, supplier, facts, since),
        [OrderRow(f, ratings.get(f.id), _rater(raters, ratings.get(f.id))) for f in facts],
        quotes,
        setup.can(db, actor, RATE),
    )


def rate(
    db: Session,
    actor: Actor,
    supplier_id: uuid.UUID,
    order_id: uuid.UUID,
    *,
    score: str,
    comment: str,
) -> None:
    """Rate (or re-rate) one of the supplier's orders once it's been received."""
    setup.require(db, actor, RATE)
    facts = next(
        (
            f
            for f in finance.supplier_history(db, supplier_id, since=SINCE_EVER)
            if f.id == order_id
        ),
        None,
    )
    if facts is None:
        raise NotFound()
    if facts.status not in RATEABLE:
        raise Conflict("Rate an order once it has been received in full or closed.")
    try:
        value = int(score)
    except ValueError:
        value = 0
    if not 1 <= value <= 5:
        raise ValidationFailed(field_errors={f"score_{order_id}": "Choose 1 to 5 stars."})
    row = db.scalars(select(SupplierRating).where(SupplierRating.order_id == order_id)).first()
    before = row.score if row else None
    if row is None:
        row = SupplierRating(supplier_id=supplier_id, order_id=order_id, score=value)
        db.add(row)
    row.score, row.comment = value, comment.strip() or None
    row.rated_by, row.rated_at = actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "procurement.supplier.rate",
        entity_type="purchase_order",
        entity_id=str(order_id),
        changes={"score": {"from": before, "to": value}},
    )
