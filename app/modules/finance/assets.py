"""Fixed assets (ADR 0027): categories, the register, assets from bills, and revaluations
and disposals recorded by one person and approved by another."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, banking, depreciation, journals, periods
from app.modules.finance.banking import can, require, uuid_or_none
from app.modules.finance.depreciation import (
    APPROVE,
    MANAGE,
    READ,
    add_months,
    first_of,
    months_between,
)
from app.modules.finance.models import (
    DEPRECIATION_METHODS,
    Account,
    AssetCategory,
    AssetChange,
    BankAccount,
    Bill,
    BillLine,
    DepreciationLine,
    DepreciationRun,
    FinanceSettings,
    FixedAsset,
    Journal,
    Supplier,
)
from app.org.models import CostCentre

ZERO = Decimal(0)
METHOD_LABELS = {
    "straight_line": "Straight line",
    "reducing_balance": "Reducing balance",
    "none": "Not depreciated",
}
CHANGE_LABELS = {"revaluation": "Revaluation", "disposal": "Disposal"}
STATUS_LABELS = {
    "active": "Active",
    "disposed": "Disposed",
    "pending_revaluation": "Revaluation waiting",
    "pending_disposal": "Disposal waiting",
    "fully_depreciated": "Fully depreciated",
}
STATUS_TONES = {
    "active": "success",
    "disposed": "neutral",
    "pending_revaluation": "warning",
    "pending_disposal": "warning",
    "fully_depreciated": "neutral",
}


def _int(value: str, key: str, errors: dict[str, str], message: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        errors[key] = message
        return None
    return number


def _rate(value: str, errors: dict[str, str]) -> Decimal | None:
    text = value.strip().rstrip("%").strip()
    if not text:
        return None
    try:
        rate = Decimal(text)
    except InvalidOperation:
        errors["rate"] = "Enter the annual rate, like 25."
        return None
    if not rate.is_finite() or rate != rate.quantize(Decimal("0.01")):
        errors["rate"] = "Enter the annual rate, like 25."
        return None
    return rate


def _method_figures(
    method: str, life: str, rate: str, errors: dict[str, str]
) -> tuple[int | None, Decimal | None]:
    """The useful life (straight line) or annual rate (reducing balance) a method needs."""
    if method == "straight_line":
        months = _int(life, "life_months", errors, "Enter the useful life in months.")
        if "life_months" not in errors and (months is None or not 1 <= months <= 1200):
            errors["life_months"] = "Enter the useful life in months, from 1 to 1,200."
        return months, None
    if method == "reducing_balance":
        value = _rate(rate, errors)
        if "rate" not in errors and (value is None or not ZERO < value <= 100):
            errors["rate"] = "Enter an annual rate above 0 and up to 100%."
        return None, value
    return None, None


# ------------------------------------------------------------------ categories and settings


@dataclass(frozen=True, slots=True)
class CategoryInput:
    name: str
    method: str
    life_months: str
    rate: str
    cost_account_id: str
    accumulated_account_id: str
    expense_account_id: str
    is_active: bool = True


def _account_of(
    db: Session, value: str, key: str, types: tuple[str, ...], errors: dict[str, str]
) -> uuid.UUID | None:
    account_id = uuid_or_none(value)
    account = db.get(Account, account_id) if account_id else None
    if account is None or account.is_header or not account.is_active or account.type not in types:
        errors[key] = "Choose an active account that takes postings."
        return None
    return account.id


def save_category(
    db: Session, actor: Actor, category_id: uuid.UUID | None, data: CategoryInput
) -> uuid.UUID:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    name = data.name.strip()
    if not name:
        errors["name"] = "Enter the category's name."
    elif db.scalar(
        select(AssetCategory.id).where(
            func.lower(AssetCategory.name) == name.lower(), AssetCategory.id != category_id
        )
    ):
        errors["name"] = "There's already a category with this name."
    if data.method not in DEPRECIATION_METHODS:
        errors["method"] = "Choose how it depreciates."
    life, rate = _method_figures(data.method, data.life_months, data.rate, errors)
    cost = _account_of(db, data.cost_account_id, "cost_account_id", ("asset",), errors)
    accumulated = expense = None
    if data.method != "none":
        accumulated = _account_of(
            db, data.accumulated_account_id, "accumulated_account_id", ("asset",), errors
        )
        expense = _account_of(
            db, data.expense_account_id, "expense_account_id", ("expense",), errors
        )
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert cost is not None  # noqa: S101
    row = db.get(AssetCategory, category_id) if category_id else None
    if category_id and row is None:
        raise NotFound()
    if row is None:
        row = AssetCategory(name=name, method=data.method, cost_account_id=cost)
        db.add(row)
    row.name, row.method, row.life_months, row.rate = name, data.method, life, rate
    row.cost_account_id, row.accumulated_account_id, row.expense_account_id = (
        cost,
        accumulated,
        expense,
    )
    row.is_active = data.is_active
    db.flush()
    record_by(
        db,
        actor,
        "finance.asset_category.save",
        entity_type="asset_category",
        entity_id=str(row.id),
        changes={"name": name, "method": data.method},
    )
    return row.id


@dataclass(frozen=True, slots=True)
class CategoryRow:
    category: AssetCategory
    life_or_rate: str
    cost: str
    accumulated: str
    expense: str
    assets: int


def _life_or_rate(method: str, life: int | None, rate: Decimal | None) -> str:
    if method == "straight_line" and life:
        years, months = divmod(life, 12)
        if not months:
            return f"{years} year{'s' if years != 1 else ''}"
        return f"{life} months"
    if method == "reducing_balance" and rate is not None:
        return f"{rate.normalize():f}% a year"
    return "—"


def categories(db: Session, actor: Actor) -> list[CategoryRow]:
    require(db, actor, READ)
    labels = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    counts = dict(
        db.execute(
            select(FixedAsset.category_id, func.count())
            .where(FixedAsset.status == "active")
            .group_by(FixedAsset.category_id)
        ).all()
    )
    return [
        CategoryRow(
            c,
            _life_or_rate(c.method, c.life_months, c.rate),
            labels.get(c.cost_account_id, ""),
            labels.get(c.accumulated_account_id, "") if c.accumulated_account_id else "—",
            labels.get(c.expense_account_id, "") if c.expense_account_id else "—",
            counts.get(c.id, 0),
        )
        for c in db.scalars(select(AssetCategory).order_by(AssetCategory.name))
    ]


def category_choices(db: Session) -> list[AssetCategory]:
    return list(
        db.scalars(
            select(AssetCategory).where(AssetCategory.is_active).order_by(AssetCategory.name)
        )
    )


@dataclass(frozen=True, slots=True)
class SettingsInput:
    threshold: str
    reserve_code: str
    disposal_code: str
    loss_code: str


def save_settings(db: Session, actor: Actor, data: SettingsInput) -> None:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    threshold = journals.parse_amount(data.threshold, "threshold", errors)
    found = accounts.by_codes(db, {data.reserve_code, data.disposal_code, data.loss_code})
    wanted = (
        ("reserve_code", data.reserve_code, ("equity",)),
        ("disposal_code", data.disposal_code, ("income", "expense")),
        ("loss_code", data.loss_code, ("expense", "income")),
    )
    for key, code, types in wanted:
        account = found.get(code.strip())
        if account is None or account.is_header or account.type not in types:
            kinds = " or ".join(types)
            errors[key] = f"Enter the code of an {kinds} account that takes postings."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = periods.settings(db)
    row.capitalisation_threshold = threshold
    row.revaluation_reserve_code = data.reserve_code.strip()
    row.disposal_code = data.disposal_code.strip()
    row.revaluation_loss_code = data.loss_code.strip()
    db.flush()
    record_by(
        db,
        actor,
        "finance.asset_settings.save",
        entity_type="finance_settings",
        entity_id="1",
        changes={"threshold": str(threshold)},
    )


# ------------------------------------------------------------------ the register


@dataclass(frozen=True, slots=True)
class AssetInput:
    name: str
    category_id: str
    acquired_on: date | None
    cost: str
    residual: str = ""
    life_months: str = ""
    rate: str = ""
    cost_centre_id: str = ""
    serial: str = ""
    location: str = ""
    description: str = ""
    opening_accumulated: str = ""
    opening_to: date | None = None  # any day in the last month already depreciated
    bill_line_id: str = ""


def _bill_line(db: Session, value: str, errors: dict[str, str]) -> tuple[BillLine, Bill] | None:
    line_id = uuid_or_none(value)
    found = (
        db.execute(
            select(BillLine, Bill)
            .join(Bill, Bill.id == BillLine.bill_id)
            .where(BillLine.id == line_id)
        ).first()
        if line_id
        else None
    )
    if found is None or found[1].kind != "bill" or found[1].status != "posted":
        errors["form"] = "That bill line isn't on a posted bill."
        return None
    if db.scalar(select(FixedAsset.id).where(FixedAsset.bill_line_id == line_id)):
        errors["form"] = "That bill line is already in the register."
        return None
    return found[0], found[1]


def _posted(db: Session, asset_id: uuid.UUID) -> bool:
    """Whether anything has been posted for the asset (depreciation or a change)."""
    return bool(
        db.scalar(
            select(
                exists()
                .where(DepreciationLine.asset_id == asset_id)
                .where(DepreciationLine.run_id == DepreciationRun.id)
                .where(DepreciationRun.status == "posted")
            )
        )
        or db.scalar(
            select(exists().where(AssetChange.asset_id == asset_id, AssetChange.status == "posted"))
        )
    )


@dataclass(frozen=True, slots=True)
class _Figures:
    category: AssetCategory
    source: tuple[BillLine, Bill] | None
    cost: Decimal
    residual: Decimal
    acquired: date
    life: int | None
    rate: Decimal | None
    opening: Decimal
    opening_to: date | None


def _check_cost(
    data: AssetInput,
    asset: FixedAsset | None,
    source: tuple[BillLine, Bill] | None,
    errors: dict[str, str],
) -> tuple[Decimal, Decimal]:
    """The cost (a bill line's net, if it came from one) and the residual value."""
    cost = journals.parse_amount(data.cost, "cost", errors)
    if source is not None:
        cost = source[0].net
    elif asset is not None and asset.bill_line_id:
        cost = asset.cost
    elif "cost" not in errors and cost <= 0:
        errors["cost"] = "Enter what it cost."
    residual = journals.parse_amount(data.residual, "residual", errors)
    if "residual" not in errors and "cost" not in errors and residual >= cost > 0:
        errors["residual"] = "The residual value must be less than the cost."
    return cost, residual


def _check_opening(
    data: AssetInput, method: str, acquired: date, room: Decimal, errors: dict[str, str]
) -> tuple[Decimal, date | None]:
    """Depreciation already charged before the asset came into the register."""
    opening = journals.parse_amount(data.opening_accumulated, "opening_accumulated", errors)
    opening_to = first_of(data.opening_to) if data.opening_to else None
    if opening <= 0 or errors:
        return ZERO, None
    if method == "none":
        errors["opening_accumulated"] = "This category isn't depreciated."
    elif opening_to is None:
        errors["opening_to"] = "Enter the last month already depreciated."
    elif not first_of(acquired) <= opening_to < first_of(clock.today()):
        errors["opening_to"] = "Choose a month from the one acquired to last month."
    elif opening > room:
        errors["opening_accumulated"] = "That's more than the cost less the residual."
    return opening, opening_to


def _figures(
    db: Session, data: AssetInput, asset: FixedAsset | None, errors: dict[str, str]
) -> _Figures:
    category_id = uuid_or_none(data.category_id)
    category = db.get(AssetCategory, category_id) if category_id else None
    if category is None or not category.is_active:
        errors["category_id"] = "Choose a category."
    source = None
    if data.bill_line_id and asset is None:
        source = _bill_line(db, data.bill_line_id, errors)
    cost, residual = _check_cost(data, asset, source, errors)
    acquired = data.acquired_on or (source[1].bill_date if source else None)
    if acquired is None:
        errors["acquired_on"] = "Enter the date it was ready for use."
    elif acquired > clock.today():
        errors["acquired_on"] = "The date can't be in the future."
    method = category.method if category else "none"
    life, rate = _method_figures(method, data.life_months, data.rate, errors)
    if source is not None and category and source[0].account_id != category.cost_account_id:
        errors["category_id"] = "Choose a category whose cost account is the bill line's."
    opening: Decimal = ZERO
    opening_to: date | None = None
    if acquired is not None:
        opening, opening_to = _check_opening(data, method, acquired, cost - residual, errors)
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert category is not None and acquired is not None  # noqa: S101
    return _Figures(category, source, cost, residual, acquired, life, rate, opening, opening_to)


def register(
    db: Session, actor: Actor, asset_id: uuid.UUID | None, data: AssetInput
) -> tuple[uuid.UUID, str | None]:
    """Register an asset, or change one. Returns its id and a warning, if any. Its money
    figures, date and category can change only until something is posted for it."""
    require(db, actor, MANAGE)
    asset = db.get(FixedAsset, asset_id) if asset_id else None
    if asset_id and asset is None:
        raise NotFound()
    errors: dict[str, str] = {}
    name = data.name.strip()
    if not name:
        errors["name"] = "Enter the asset's name."
    centre = uuid_or_none(data.cost_centre_id)
    if centre and db.get(CostCentre, centre) is None:
        errors["cost_centre_id"] = "Choose a cost centre."
    if asset is not None and (asset.status != "active" or _posted(db, asset.id)):
        if errors:
            raise ValidationFailed(field_errors=errors)
        _describe(asset, name, data, centre)
        db.flush()
        record_by(
            db, actor, "finance.asset.update", entity_type="fixed_asset", entity_id=str(asset.id)
        )
        return asset.id, None
    f = _figures(db, data, asset, errors)
    category = f.category
    if asset is None:
        asset = FixedAsset(
            number=numbering.next_number(db, "fixed_asset"),
            created_by=actor.user_id,
            category_id=category.id,
            method=category.method,
            cost_account_id=category.cost_account_id,
            acquired_on=f.acquired,
            cost=f.cost,
            gross=f.cost,
        )
        if f.source is not None:
            line, bill = f.source
            asset.bill_line_id, asset.supplier_id = line.id, bill.supplier_id
            centre = centre or line.cost_centre_id
        db.add(asset)
    asset.category_id, asset.method = category.id, category.method
    asset.life_months, asset.rate = f.life, f.rate
    asset.cost_account_id = category.cost_account_id
    asset.accumulated_account_id = category.accumulated_account_id
    asset.expense_account_id = category.expense_account_id
    asset.acquired_on, asset.cost, asset.gross = f.acquired, f.cost, f.cost
    asset.residual = f.residual
    charged = 0
    if f.opening > 0 and f.opening_to is not None:
        charged = months_between(first_of(f.acquired), f.opening_to) + 1
    asset.accumulated = f.opening if charged else ZERO
    asset.depreciated_to = f.opening_to if charged else None
    asset.months_left = (
        max((f.life or 0) - charged, 0) if category.method == "straight_line" else None
    )
    _describe(asset, name, data, centre)
    db.flush()
    record_by(
        db,
        actor,
        "finance.asset.register" if asset_id is None else "finance.asset.update",
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        changes={"number": asset.number, "cost": str(f.cost)},
    )
    threshold = periods.settings(db).capitalisation_threshold
    warning = None
    if f.cost < threshold:
        warning = (
            f"Its cost is below the capitalisation threshold of ₦{threshold:,.2f}. "
            "Check it belongs in the register."
        )
    return asset.id, warning


def _describe(asset: FixedAsset, name: str, data: AssetInput, centre: uuid.UUID | None) -> None:
    asset.name = name[:200]
    asset.serial = data.serial.strip()[:120] or None
    asset.location = data.location.strip()[:120] or None
    asset.description = data.description.strip() or None
    asset.cost_centre_id = centre


def delete_asset(db: Session, actor: Actor, asset_id: uuid.UUID) -> None:
    require(db, actor, MANAGE)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        raise NotFound()
    if _posted(db, asset.id) or db.scalar(
        select(AssetChange.id).where(AssetChange.asset_id == asset.id)
    ):
        raise Conflict("Something has been recorded for this asset, so it stays in the register.")
    db.execute(delete(DepreciationLine).where(DepreciationLine.asset_id == asset.id))
    record_by(
        db,
        actor,
        "finance.asset.delete",
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        changes={"number": asset.number},
    )
    db.delete(asset)
    db.flush()


# ------------------------------------------------------------------ revaluations and disposals


def record_change(
    db: Session,
    actor: Actor,
    asset_id: uuid.UUID,
    *,
    kind: str,
    on: date | None,
    amount: str,
    bank_id: str,
    reason: str,
) -> uuid.UUID:
    require(db, actor, MANAGE)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        raise NotFound()
    if asset.status != "active":
        raise Conflict("This asset has been disposed of.")
    if db.scalar(
        select(AssetChange.id).where(
            AssetChange.asset_id == asset.id, AssetChange.status == "draft"
        )
    ):
        raise Conflict("A change to this asset is already waiting for approval.")
    if kind not in CHANGE_LABELS:
        raise Conflict("Choose a revaluation or a disposal.")
    errors: dict[str, str] = {}
    if on is None:
        errors["on"] = "Enter the date."
    elif on > clock.today():
        errors["on"] = "The date can't be in the future."
    elif on < asset.acquired_on:
        errors["on"] = "The date can't be before the asset was acquired."
    value = journals.parse_amount(amount, "amount", errors)
    bank = uuid_or_none(bank_id)
    if kind == "revaluation" and "amount" not in errors and value <= 0:
        errors["amount"] = "Enter the new value."
    if kind == "disposal" and value > 0 and bank not in {b for b, _ in banking.choices(db)}:
        errors["bank_id"] = "Choose the bank account the proceeds went into."
    if not reason.strip():
        errors["reason"] = "Say why."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert on is not None  # noqa: S101
    change = AssetChange(
        asset_id=asset.id,
        kind=kind,
        change_date=on,
        amount=value,
        bank_account_id=bank if kind == "disposal" and value > 0 else None,
        reason=reason.strip(),
        created_by=actor.user_id,
    )
    db.add(change)
    db.flush()
    record_by(
        db,
        actor,
        f"finance.asset.{kind}.record",
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        changes={"date": on.isoformat(), "amount": str(value)},
    )
    return change.id


def _locked_change(db: Session, change_id: uuid.UUID) -> AssetChange:
    change = db.scalars(
        select(AssetChange).where(AssetChange.id == change_id).with_for_update()
    ).first()
    if change is None:
        raise NotFound()
    return change


def delete_change(db: Session, actor: Actor, change_id: uuid.UUID) -> uuid.UUID:
    require(db, actor, MANAGE)
    change = _locked_change(db, change_id)
    if change.status != "draft":
        raise Conflict("An approved change can't be deleted.")
    record_by(
        db,
        actor,
        f"finance.asset.{change.kind}.delete",
        entity_type="fixed_asset",
        entity_id=str(change.asset_id),
    )
    db.delete(change)
    db.flush()
    return change.asset_id


def _code(db: Session, account_id: uuid.UUID | None) -> str:
    account = db.get(Account, account_id) if account_id else None
    if account is None:
        raise Conflict("The asset's accounts are missing.")
    return account.code


def _lines(pairs: list[tuple[str, Decimal]]) -> list[journals.SystemLine]:
    """(code, signed amount: debit positive) into journal lines, leaving out zeros."""
    return [
        (code, None, amount if amount > 0 else ZERO, -amount if amount < 0 else ZERO)
        for code, amount in pairs
        if amount != 0
    ]


def _cleared(asset: FixedAsset, accumulated_code: str | None) -> list[tuple[str, Decimal]]:
    """Clear the accumulated depreciation (debit it)."""
    if asset.accumulated <= 0:
        return []
    assert accumulated_code is not None  # noqa: S101
    return [(accumulated_code, asset.accumulated)]


def _revaluation_pairs(
    asset: FixedAsset,
    change: AssetChange,
    *,
    settings: FinanceSettings,
    cost_code: str,
    accumulated_code: str | None,
) -> tuple[list[tuple[str, Decimal]], Decimal]:
    """IAS 16, elimination method: an increase reverses earlier losses through profit, then
    goes to the reserve; a decrease uses the reserve, then goes to profit. Updates the
    asset's reserve and losses."""
    result = change.amount - (asset.gross - asset.accumulated)
    pairs = [*_cleared(asset, accumulated_code), (cost_code, change.amount - asset.gross)]
    if result > 0:
        back = min(result, asset.revaluation_losses)
        pairs += [
            (settings.revaluation_loss_code, -back),
            (settings.revaluation_reserve_code, -(result - back)),
        ]
        asset.revaluation_losses -= back
        asset.reserve += result - back
    elif result < 0:
        used = min(-result, asset.reserve)
        pairs += [
            (settings.revaluation_reserve_code, used),
            (settings.revaluation_loss_code, -result - used),
        ]
        asset.reserve -= used
        asset.revaluation_losses += -result - used
    return pairs, result


def _disposal_pairs(
    db: Session,
    asset: FixedAsset,
    change: AssetChange,
    *,
    settings: FinanceSettings,
    cost_code: str,
    accumulated_code: str | None,
) -> tuple[list[tuple[str, Decimal]], Decimal]:
    """Clear the cost and depreciation, take in the proceeds, the gain or loss to profit,
    and the asset's revaluation reserve to retained earnings."""
    result = change.amount - (asset.gross - asset.accumulated)
    pairs = _cleared(asset, accumulated_code)
    if change.amount > 0:
        bank = db.get(BankAccount, change.bank_account_id)
        if bank is None:
            raise Conflict("Choose the bank account the proceeds went into.")
        pairs.append((banking.ledger_of(db, bank.id).code, change.amount))
    pairs += [(cost_code, -asset.gross), (settings.disposal_code, -result)]
    if asset.reserve > 0:
        pairs += [
            (settings.revaluation_reserve_code, asset.reserve),
            (settings.retained_earnings_code, -asset.reserve),
        ]
    return pairs, result


def approve_change(db: Session, actor: Actor, change_id: uuid.UUID) -> uuid.UUID:
    require(db, actor, APPROVE)
    change = _locked_change(db, change_id)
    if change.status != "draft":
        raise Conflict("This change has already been approved.")
    if depreciation.sod_blocked(db, actor, change.created_by):
        raise PermissionDenied("You recorded this change, so someone else approves it.")
    asset = db.scalars(
        select(FixedAsset).where(FixedAsset.id == change.asset_id).with_for_update()
    ).one()
    if asset.status != "active":
        raise Conflict("This asset has been disposed of.")
    problem = depreciation.unposted_before(asset, change.change_date)
    if problem:
        raise Conflict(problem)
    settings = periods.settings(db)
    carrying = asset.gross - asset.accumulated
    cost_code = _code(db, asset.cost_account_id)
    accumulated_code = (
        _code(db, asset.accumulated_account_id) if asset.accumulated_account_id else None
    )
    if change.kind == "revaluation":
        pairs, result = _revaluation_pairs(
            asset, change, settings=settings, cost_code=cost_code, accumulated_code=accumulated_code
        )
        memo = f"Revaluation of {asset.number} {asset.name}"
    else:
        pairs, result = _disposal_pairs(
            db,
            asset,
            change,
            settings=settings,
            cost_code=cost_code,
            accumulated_code=accumulated_code,
        )
        memo = f"Disposal of {asset.number} {asset.name}"
    lines = _lines(pairs)
    if len(lines) < 2:
        raise Conflict("There's nothing to post: the amount equals the carrying amount.")
    journal = journals.post_document_journal(
        db,
        source_id=change.id,
        journal_date=change.change_date,
        memo=memo,
        lines=lines,
        poster=actor.user_id,
        source="asset",
    )
    month = first_of(change.change_date)
    if change.kind == "revaluation":
        asset.gross, asset.accumulated = change.amount, ZERO
        asset.residual = min(asset.residual, change.amount)
        if first_of(asset.acquired_on) < month:
            asset.depreciated_to = add_months(month, -1)
    else:
        asset.status, asset.disposed_on, asset.reserve = "disposed", change.change_date, ZERO
    change.carrying, change.result = carrying, result
    change.status, change.approved_by, change.approved_at = "posted", actor.user_id, clock.now()
    change.journal_id = journal.id
    db.flush()
    record_by(
        db,
        actor,
        f"finance.asset.{change.kind}.approve",
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        changes={"journal": journal.number or "", "result": str(result)},
    )
    return asset.id


# ------------------------------------------------------------------ views


def _display_status(asset: FixedAsset, waiting: str | None) -> str:
    if asset.status == "disposed":
        return "disposed"
    if waiting:
        return f"pending_{waiting}"
    if asset.method != "none" and asset.gross - asset.accumulated <= asset.residual:
        return "fully_depreciated"
    return "active"


@dataclass(frozen=True, slots=True)
class RegisterRow:
    id: uuid.UUID
    number: str
    name: str
    serial: str | None
    category: str
    acquired_on: date
    gross: Decimal
    accumulated: Decimal
    book_value: Decimal
    status: str


def register_rows(
    db: Session, actor: Actor, *, category: str = "", status: str = "active", q: str = ""
) -> list[RegisterRow]:
    require(db, actor, READ)
    query = select(FixedAsset).order_by(FixedAsset.number)
    if status in ("active", "disposed"):
        query = query.where(FixedAsset.status == status)
    if uuid_or_none(category):
        query = query.where(FixedAsset.category_id == uuid_or_none(category))
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(
            func.lower(FixedAsset.name).like(like)
            | func.lower(FixedAsset.number).like(like)
            | func.lower(func.coalesce(FixedAsset.serial, "")).like(like)
        )
    names = dict(db.execute(select(AssetCategory.id, AssetCategory.name)).all())
    waiting = dict(
        db.execute(
            select(AssetChange.asset_id, AssetChange.kind).where(AssetChange.status == "draft")
        ).all()
    )
    return [
        RegisterRow(
            a.id,
            a.number,
            a.name,
            a.serial,
            names.get(a.category_id, ""),
            a.acquired_on,
            a.gross,
            a.accumulated,
            a.gross - a.accumulated,
            _display_status(a, waiting.get(a.id)),
        )
        for a in db.scalars(query.limit(500))
    ]


@dataclass(frozen=True, slots=True)
class Totals:
    gross: Decimal
    accumulated: Decimal
    book_value: Decimal
    depreciated_to: date | None


def totals(db: Session) -> Totals:
    gross, accumulated = db.execute(
        select(
            func.coalesce(func.sum(FixedAsset.gross), 0),
            func.coalesce(func.sum(FixedAsset.accumulated), 0),
        ).where(FixedAsset.status == "active")
    ).one()
    gross, accumulated = Decimal(gross), Decimal(accumulated)
    return Totals(gross, accumulated, gross - accumulated, depreciation.last_posted(db))


@dataclass(frozen=True, slots=True)
class BillCandidate:
    line_id: uuid.UUID
    bill_id: uuid.UUID
    bill_number: str
    bill_date: date
    supplier: str
    description: str
    net: Decimal
    account: str
    category_id: uuid.UUID


def from_bills(db: Session, actor: Actor) -> list[BillCandidate]:
    """Posted bill lines on a category's cost account, at or above the threshold, not
    yet in the register."""
    require(db, actor, READ)
    threshold = periods.settings(db).capitalisation_threshold
    by_account: dict[uuid.UUID, uuid.UUID] = {}
    for c in category_choices(db):
        by_account.setdefault(c.cost_account_id, c.id)
    if not by_account:
        return []
    labels = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    rows = db.execute(
        select(BillLine, Bill, Supplier.name)
        .join(Bill, Bill.id == BillLine.bill_id)
        .join(Supplier, Supplier.id == Bill.supplier_id)
        .where(
            Bill.kind == "bill",
            Bill.status == "posted",
            BillLine.account_id.in_(list(by_account)),
            BillLine.net >= threshold,
            ~exists().where(FixedAsset.bill_line_id == BillLine.id),
        )
        .order_by(Bill.bill_date.desc(), BillLine.position)
        .limit(50)
    ).all()
    return [
        BillCandidate(
            line.id,
            bill.id,
            bill.number or "",
            bill.bill_date,
            supplier,
            line.description,
            line.net,
            labels.get(line.account_id, ""),
            by_account[line.account_id],
        )
        for line, bill, supplier in rows
    ]


def bill_candidate(db: Session, actor: Actor, line_id: uuid.UUID) -> BillCandidate | None:
    return next((c for c in from_bills(db, actor) if c.line_id == line_id), None)


@dataclass(frozen=True, slots=True)
class LedgerCheck:
    account: str
    register: Decimal
    ledger: Decimal

    @property
    def matches(self) -> bool:
        return self.register == self.ledger


def ledger_check(db: Session, actor: Actor) -> list[LedgerCheck]:
    """The register's cost or valuation against the ledger, per cost account."""
    require(db, actor, READ)
    used = {c.cost_account_id for c in db.scalars(select(AssetCategory))}
    register = dict(
        db.execute(
            select(FixedAsset.cost_account_id, func.sum(FixedAsset.gross))
            .where(FixedAsset.status == "active")
            .group_by(FixedAsset.cost_account_id)
        ).all()
    )
    out = []
    for account in sorted(
        (a for a in accounts.all_accounts(db) if a.id in used), key=lambda a: a.code
    ):
        ledger = banking.ledger_balance(db, account.id)
        mine = Decimal(register.get(account.id, 0) or 0)
        if mine or ledger:
            out.append(LedgerCheck(f"{account.code} {account.name}", mine, ledger))
    return out


@dataclass(frozen=True, slots=True)
class ChangeView:
    id: uuid.UUID
    kind: str
    status: str
    change_date: date
    amount: Decimal
    bank: str | None
    reason: str
    carrying: Decimal  # at approval, or now while waiting
    result: Decimal  # gain (+) or loss (-)
    created_by: str | None
    created_at: datetime
    approved_by: str | None
    journal: tuple[uuid.UUID, str] | None
    can_approve: bool
    approve_blocked: str | None
    can_delete: bool


@dataclass(frozen=True, slots=True)
class HistoryRow:
    month: date
    months: int
    amount: Decimal
    journal: tuple[uuid.UUID, str] | None


@dataclass(frozen=True, slots=True)
class AssetView:
    asset: FixedAsset
    category: str
    method: str
    accounts: str
    cost_centre: str | None
    supplier: str | None
    bill: tuple[uuid.UUID, str] | None
    book_value: Decimal
    status: str
    history: list[HistoryRow]
    projection: list[tuple[date, Decimal]]
    changes: list[ChangeView]
    waiting: ChangeView | None
    can_manage: bool
    can_change: bool
    figures_locked: bool
    can_delete: bool


def _change_view(
    db: Session, actor: Actor, change: AssetChange, asset: FixedAsset, names: dict[uuid.UUID, str]
) -> ChangeView:
    journal = db.get(Journal, change.journal_id) if change.journal_id else None
    carrying = change.carrying if change.carrying is not None else asset.gross - asset.accumulated
    result = change.result if change.result is not None else change.amount - carrying
    bank = None
    if change.bank_account_id:
        ledger = banking.ledger_of(db, change.bank_account_id)
        bank = f"{ledger.code} {ledger.name}"
    allowed = change.status == "draft" and can(db, actor, APPROVE)
    blocked = None
    if allowed and depreciation.sod_blocked(db, actor, change.created_by):
        allowed, blocked = False, "You recorded this, so someone else approves it."
    return ChangeView(
        change.id,
        change.kind,
        change.status,
        change.change_date,
        change.amount,
        bank,
        change.reason,
        carrying,
        result,
        names.get(change.created_by) if change.created_by else None,
        change.created_at,
        names.get(change.approved_by) if change.approved_by else None,
        (journal.id, journal.number) if journal and journal.number else None,
        allowed,
        blocked,
        change.status == "draft" and can(db, actor, MANAGE),
    )


def view(db: Session, actor: Actor, asset_id: uuid.UUID) -> AssetView:
    require(db, actor, READ)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        raise NotFound()
    category = db.get(AssetCategory, asset.category_id)
    labels = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    account_text = " / ".join(
        labels[i].split(" ", 1)[0]
        for i in (asset.cost_account_id, asset.accumulated_account_id, asset.expense_account_id)
        if i and i in labels
    )
    centre = db.get(CostCentre, asset.cost_centre_id) if asset.cost_centre_id else None
    supplier = db.get(Supplier, asset.supplier_id) if asset.supplier_id else None
    bill = None
    if asset.bill_line_id:
        found = db.execute(
            select(Bill.id, Bill.number)
            .join(BillLine, BillLine.bill_id == Bill.id)
            .where(BillLine.id == asset.bill_line_id)
        ).first()
        bill = (found[0], found[1] or "") if found else None
    history = [
        HistoryRow(
            run.month,
            line.months,
            line.amount,
            (journal.id, journal.number) if journal and journal.number else None,
        )
        for line, run, journal in db.execute(
            select(DepreciationLine, DepreciationRun, Journal)
            .join(DepreciationRun, DepreciationRun.id == DepreciationLine.run_id)
            .outerjoin(Journal, Journal.id == DepreciationRun.journal_id)
            .where(DepreciationLine.asset_id == asset.id, DepreciationRun.status == "posted")
            .order_by(DepreciationRun.month.desc())
        ).all()
    ]
    projection: list[tuple[date, Decimal]] = []
    if asset.status == "active" and asset.method != "none":
        start = depreciation.start_month(asset)
        charge = depreciation.simulate(asset, 6)
        projection = [
            (add_months(start, i), amount) for i, amount in enumerate(charge.schedule) if amount > 0
        ]
    changes = list(
        db.scalars(
            select(AssetChange)
            .where(AssetChange.asset_id == asset.id)
            .order_by(AssetChange.created_at.desc(), AssetChange.id.desc())
        )
    )
    names = journals.user_names(
        db, {c.created_by for c in changes} | {c.approved_by for c in changes}
    )
    change_views = [_change_view(db, actor, c, asset, names) for c in changes]
    waiting = next((c for c in change_views if c.status == "draft"), None)
    manage = can(db, actor, MANAGE)
    posted = _posted(db, asset.id)
    return AssetView(
        asset,
        category.name if category else "",
        depreciation_text(asset),
        account_text,
        centre.name if centre else None,
        supplier.name if supplier else None,
        bill,
        asset.gross - asset.accumulated,
        _display_status(asset, waiting.kind if waiting else None),
        history,
        projection,
        change_views,
        waiting,
        manage,
        manage and asset.status == "active" and waiting is None,
        posted or asset.status != "active",
        manage and not posted and not changes,
    )


def depreciation_text(asset: FixedAsset) -> str:
    text = METHOD_LABELS[asset.method]
    if asset.method == "straight_line" and asset.life_months:
        return f"{text} · {asset.life_months} months · {asset.months_left or 0} left"
    if asset.method == "reducing_balance" and asset.rate is not None:
        return f"{text} · {asset.rate.normalize():f}% a year"
    return text
