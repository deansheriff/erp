"""Monthly depreciation (ADR 0027): a full month's charge in the month an asset is acquired
and none in the month it's disposed of. A run covers one month and charges every month not
yet charged up to it; someone other than whoever prepared it posts it."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import catalogue, engine, sod
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied
from app.identity.schemas import Actor
from app.modules.finance import journals
from app.modules.finance.banking import can, require
from app.modules.finance.models import (
    Account,
    AssetCategory,
    DepreciationLine,
    DepreciationRun,
    FixedAsset,
    Journal,
)

READ = "finance.asset.read"
MANAGE = "finance.asset.manage"
APPROVE = "finance.asset.approve"
CENT = Decimal("0.01")
ZERO = Decimal(0)
STATUS_LABELS = {"draft": "Draft", "posted": "Posted"}
STATUS_TONES = {"draft": "warning", "posted": "success"}


# ------------------------------------------------------------------ months


def first_of(day: date) -> date:
    return day.replace(day=1)


def add_months(month: date, n: int) -> date:
    years, index = divmod(month.month - 1 + n, 12)
    return date(month.year + years, index + 1, 1)


def months_between(start: date, end: date) -> int:
    """Whole months from one month's first day to another's (0 for the same month)."""
    return (end.year - start.year) * 12 + end.month - start.month


def month_end(month: date) -> date:
    return add_months(month, 1) - timedelta(days=1)


# ------------------------------------------------------------------ the charge


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Charge:
    months: int  # months covered
    amount: Decimal
    schedule: tuple[Decimal, ...]  # one charge per month covered


def simulate(asset: FixedAsset, months: int) -> Charge:
    """The charge for the next `months` months from the asset's current state, which it
    doesn't change. Straight line spreads what is left above the residual evenly over the
    months left, so rounding evens out; reducing balance takes the annual rate a month
    at a time, never below the residual."""
    carrying = asset.gross - asset.accumulated
    left = asset.months_left or 0
    out: list[Decimal] = []
    for _ in range(max(months, 0)):
        room = max(carrying - asset.residual, ZERO)
        if asset.method == "straight_line" and left > 0:
            charge = _cents(room / left)
            left -= 1
        elif asset.method == "reducing_balance" and asset.rate:
            charge = min(_cents(carrying * asset.rate / Decimal(1200)), room)
        else:
            charge = ZERO
        carrying -= charge
        out.append(charge)
    return Charge(len(out), sum(out, ZERO), tuple(out))


def start_month(asset: FixedAsset) -> date:
    """The first month not yet charged."""
    if asset.depreciated_to:
        return add_months(asset.depreciated_to, 1)
    return first_of(asset.acquired_on)


def due_through(asset: FixedAsset, month: date) -> Charge:
    """What is still to charge from the first uncharged month up to `month`."""
    return simulate(asset, months_between(start_month(asset), month) + 1)


def unposted_before(asset: FixedAsset, day: date) -> str | None:
    """Why a revaluation or disposal on `day` can't go ahead yet, or None: depreciation
    must be posted up to the month before, and not for the month itself."""
    month = first_of(day)
    if asset.depreciated_to and asset.depreciated_to >= month:
        return (
            f"Depreciation is already posted for {asset.depreciated_to:%B %Y}. "
            "Use a date after that month."
        )
    if asset.method != "none" and due_through(asset, add_months(month, -1)).amount > 0:
        return (
            f"Post the depreciation up to {add_months(month, -1):%B %Y} first, "
            "so the carrying amount is right."
        )
    return None


# ------------------------------------------------------------------ runs


def _depreciating(db: Session) -> list[FixedAsset]:
    return list(
        db.scalars(
            select(FixedAsset)
            .where(FixedAsset.status == "active", FixedAsset.method != "none")
            .order_by(FixedAsset.number)
        )
    )


def next_month(db: Session) -> date | None:
    """The month the next run covers: the one after the last posted run, or the first
    month any asset needs."""
    last = db.scalar(
        select(func.max(DepreciationRun.month)).where(DepreciationRun.status == "posted")
    )
    if last:
        return add_months(last, 1)
    starts = [start_month(a) for a in _depreciating(db)]
    return min(starts) if starts else None


def compute(db: Session, month: date) -> dict[uuid.UUID, Charge]:
    out = {}
    for asset in _depreciating(db):
        if first_of(asset.acquired_on) > month or start_month(asset) > month:
            continue
        charge = due_through(asset, month)
        if charge.amount > 0:
            out[asset.id] = charge
    return out


def draft(db: Session) -> DepreciationRun | None:
    return db.scalars(select(DepreciationRun).where(DepreciationRun.status == "draft")).first()


def prepare(
    db: Session, actor: Actor | None, *, today: date | None = None
) -> DepreciationRun | None:
    """Prepare (or recalculate) the draft run for the next month, if that month has
    started. `actor` None is the nightly task, which leaves the posting to anyone allowed."""
    if actor is not None:
        require(db, actor, MANAGE)
    today = today or clock.today()
    month = next_month(db)
    if month is None or month > first_of(today):
        return None
    run = draft(db)
    if run is not None and run.month != month:
        db.delete(run)
        db.flush()
        run = None
    if run is None:
        run = DepreciationRun(month=month)
        db.add(run)
    run.prepared_by = actor.user_id if actor else None
    run.prepared_at = clock.now()
    db.flush()
    db.execute(delete(DepreciationLine).where(DepreciationLine.run_id == run.id))
    charges = compute(db, month)
    for asset_id, charge in charges.items():
        db.add(
            DepreciationLine(
                run_id=run.id, asset_id=asset_id, months=charge.months, amount=charge.amount
            )
        )
    run.total = sum((c.amount for c in charges.values()), ZERO)
    db.flush()
    if actor is not None:
        record_by(
            db,
            actor,
            "finance.depreciation.prepare",
            entity_type="depreciation_run",
            entity_id=str(run.id),
            changes={"month": month.isoformat(), "total": str(run.total)},
        )
    return run


def draft_due(db: Session, today: date | None = None) -> bool:
    """The nightly task: draft the next run once its month has ended, unless one waits."""
    today = today or clock.today()
    month = next_month(db)
    if month is None or month_end(month) >= today or draft(db) is not None:
        return False
    return prepare(db, None, today=today) is not None


def sod_blocked(db: Session, actor: Actor, prepared_by: uuid.UUID | None) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return bool(
        sod.violations(
            catalogue.sod_rules(), APPROVE, actor.user_id, {"created_by": prepared_by}, held
        )
    )


def _lines(db: Session, run_id: uuid.UUID) -> list[DepreciationLine]:
    return list(db.scalars(select(DepreciationLine).where(DepreciationLine.run_id == run_id)))


def post(db: Session, actor: Actor, run_id: uuid.UUID) -> Journal | None:
    require(db, actor, APPROVE)
    run = db.scalars(
        select(DepreciationRun).where(DepreciationRun.id == run_id).with_for_update()
    ).first()
    if run is None:
        raise NotFound()
    if run.status != "draft":
        raise Conflict("This run has already been posted.")
    if sod_blocked(db, actor, run.prepared_by):
        raise PermissionDenied("You prepared this run, so someone else posts it.")
    if next_month(db) != run.month:
        raise Conflict("Runs are posted month by month. Recalculate this run first.")
    lines = _lines(db, run.id)
    stored = {line.asset_id: (line.months, line.amount) for line in lines}
    fresh = {k: (c.months, c.amount) for k, c in compute(db, run.month).items()}
    if stored != fresh:
        raise Conflict(
            "The assets have changed since this run was prepared. Recalculate it, "
            "then someone other than you posts it."
        )
    assets = {
        a.id: a
        for a in db.scalars(
            select(FixedAsset).where(FixedAsset.id.in_(list(stored))).with_for_update()
        )
    }
    journal = None
    if lines:
        codes = dict(db.execute(select(Account.id, Account.code)).all())
        debits: dict[tuple[str, uuid.UUID | None], Decimal] = defaultdict(lambda: ZERO)
        credits: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for line in lines:
            asset = assets[line.asset_id]
            assert asset.expense_account_id and asset.accumulated_account_id  # noqa: S101
            debits[(codes[asset.expense_account_id], asset.cost_centre_id)] += line.amount
            credits[codes[asset.accumulated_account_id]] += line.amount
        journal = journals.post_document_journal(
            db,
            source_id=run.id,
            journal_date=month_end(run.month),
            memo=f"Depreciation for {run.month:%B %Y}",
            lines=[(code, centre, amount, ZERO) for (code, centre), amount in debits.items()]
            + [(code, None, ZERO, amount) for code, amount in credits.items()],
            poster=actor.user_id,
            source="asset",
        )
    for line in lines:
        asset = assets[line.asset_id]
        asset.accumulated += line.amount
        asset.depreciated_to = run.month
        if asset.months_left is not None:
            asset.months_left = max(asset.months_left - line.months, 0)
    run.status, run.posted_by, run.posted_at = "posted", actor.user_id, clock.now()
    run.journal_id = journal.id if journal else None
    db.flush()
    record_by(
        db,
        actor,
        "finance.depreciation.post",
        entity_type="depreciation_run",
        entity_id=str(run.id),
        changes={"month": run.month.isoformat(), "total": str(run.total)},
    )
    return journal


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class RunLine:
    asset_id: uuid.UUID
    number: str
    name: str
    months: int
    amount: Decimal


@dataclass(frozen=True, slots=True)
class RunGroup:
    category: str
    expense: str
    accumulated: str
    lines: list[RunLine]
    total: Decimal


@dataclass(frozen=True, slots=True)
class RunView:
    id: uuid.UUID
    month: date
    status: str
    total: Decimal
    count: int
    prepared_by: str | None  # None: the nightly task
    prepared_at: datetime
    posted_by: str | None
    posted_at: datetime | None
    journal: tuple[uuid.UUID, str] | None
    groups: list[RunGroup]
    can_post: bool
    post_blocked: str | None


def _view(db: Session, actor: Actor, run: DepreciationRun, *, detail: bool) -> RunView:
    names = journals.user_names(db, {run.prepared_by, run.posted_by})
    journal = db.get(Journal, run.journal_id) if run.journal_id else None
    groups: list[RunGroup] = []
    lines = _lines(db, run.id)
    if detail and lines:
        accounts = {a.id: a for a in db.scalars(select(Account))}
        categories = dict(db.execute(select(AssetCategory.id, AssetCategory.name)).all())
        assets = {
            a.id: a
            for a in db.scalars(
                select(FixedAsset).where(FixedAsset.id.in_([x.asset_id for x in lines]))
            )
        }
        by_category: dict[uuid.UUID, list[RunLine]] = defaultdict(list)
        for line in sorted(lines, key=lambda x: assets[x.asset_id].number):
            asset = assets[line.asset_id]
            by_category[asset.category_id].append(
                RunLine(asset.id, asset.number, asset.name, line.months, line.amount)
            )
        for category_id, rows in sorted(by_category.items(), key=lambda kv: categories[kv[0]]):
            sample = assets[rows[0].asset_id]

            def label(account_id: uuid.UUID | None) -> str:
                account = accounts.get(account_id) if account_id else None
                return f"{account.code} {account.name}" if account else ""

            groups.append(
                RunGroup(
                    categories[category_id],
                    label(sample.expense_account_id),
                    label(sample.accumulated_account_id),
                    rows,
                    sum((r.amount for r in rows), ZERO),
                )
            )
    allowed = run.status == "draft" and can(db, actor, APPROVE)
    blocked = None
    if allowed and sod_blocked(db, actor, run.prepared_by):
        allowed, blocked = False, "You prepared this run, so someone else posts it."
    return RunView(
        run.id,
        run.month,
        run.status,
        run.total,
        len(lines),
        names.get(run.prepared_by) if run.prepared_by else None,
        run.prepared_at,
        names.get(run.posted_by) if run.posted_by else None,
        run.posted_at,
        (journal.id, journal.number) if journal and journal.number else None,
        groups,
        allowed,
        blocked,
    )


def current(db: Session, actor: Actor) -> RunView | None:
    require(db, actor, READ)
    run = draft(db)
    return _view(db, actor, run, detail=True) if run else None


def posted_runs(db: Session, actor: Actor) -> list[RunView]:
    require(db, actor, READ)
    runs = db.scalars(
        select(DepreciationRun)
        .where(DepreciationRun.status == "posted")
        .order_by(DepreciationRun.month.desc())
        .limit(36)
    )
    return [_view(db, actor, r, detail=False) for r in runs]


def last_posted(db: Session) -> date | None:
    return db.scalar(
        select(func.max(DepreciationRun.month)).where(DepreciationRun.status == "posted")
    )
