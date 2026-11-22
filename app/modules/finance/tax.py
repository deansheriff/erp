"""Tax (ADR 0024): effective-dated VAT rates, withholding-tax types, stamp duty types, the
receivables settings, and monthly VAT, WHT and stamp duty returns read from the ledger.
A return is a draft with live figures until it is filed; filing freezes them."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, periods
from app.modules.finance.models import (
    STAMP_BASES,
    TAXES,
    FinanceSettings,
    FiscalYear,
    Journal,
    JournalLine,
    StampDutyType,
    TaxReturn,
    VatRate,
    WhtType,
)

READ = "finance.tax.read"
MANAGE = "finance.tax.manage"
CENT = Decimal("0.01")
ZERO = Decimal(0)
HUNDRED = Decimal(100)
TAX_LABELS = {"vat": "VAT", "wht": "Withholding tax", "stamp": "Stamp duty"}
BASIS_LABELS = {"flat": "Flat amount", "percent": "Percentage of net"}
# Settlement journals of returns (from banking, 10d) don't count towards a return.
SETTLEMENT_SOURCE = "tax"
MAX_TERMS = 365
MAX_DAYS = 90


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not _can(db, actor, permission):
        raise PermissionDenied()


def _rate(value: str, key: str, errors: dict[str, str], *, blank: bool = False) -> Decimal | None:
    text = value.strip().rstrip("%").strip()
    if not text:
        if not blank:
            errors[key] = "Enter a rate."
        return None
    try:
        rate = Decimal(text)
    except InvalidOperation:
        errors[key] = "Enter a percentage, like 7.5."
        return None
    if not rate.is_finite() or rate < 0 or rate >= HUNDRED or rate != rate.quantize(CENT):
        errors[key] = "Enter a percentage from 0 to 99.99."
        return None
    return rate


# ------------------------------------------------------------------ VAT


def vat_rates(db: Session) -> list[VatRate]:
    return list(db.scalars(select(VatRate).order_by(VatRate.effective_from.desc())))


def vat_rate_on(db: Session, day: date) -> Decimal | None:
    """The standard VAT rate in force on the day, or None if none is set yet."""
    return db.scalar(
        select(VatRate.rate)
        .where(VatRate.effective_from <= day)
        .order_by(VatRate.effective_from.desc())
        .limit(1)
    )


def vat_amount(net: Decimal, rate: Decimal) -> Decimal:
    return (net * rate / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def add_vat_rate(db: Session, actor: Actor, *, effective_from: date | None, rate: str) -> None:
    _require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    parsed = _rate(rate, "rate", errors)
    if effective_from is None:
        errors["effective_from"] = "Choose the date the rate starts."
    elif db.scalar(select(VatRate.id).where(VatRate.effective_from == effective_from)):
        errors["effective_from"] = "A rate already starts on that date."
    if errors:
        raise ValidationFailed(field_errors=errors)
    db.add(VatRate(effective_from=effective_from, rate=parsed))
    db.flush()
    record_by(
        db,
        actor,
        "finance.tax.vat_rate",
        entity_type="vat_rate",
        changes={"rate": {"from": None, "to": str(parsed)}, "from": str(effective_from)},
    )


# ------------------------------------------------------------------ withholding tax


def wht_types(db: Session, *, active_only: bool = False) -> list[WhtType]:
    query = select(WhtType).order_by(WhtType.position)
    if active_only:
        query = query.where(WhtType.is_active)
    return list(db.scalars(query))


@dataclass(frozen=True, slots=True)
class WhtRateInput:
    wht_type_id: str
    company_rate: str
    individual_rate: str
    is_active: bool


def save_wht_rates(db: Session, actor: Actor, rows: list[WhtRateInput]) -> None:
    """Set the rates of every type at once; a blank rate stays unset."""
    _require(db, actor, MANAGE)
    known = {str(t.id): t for t in wht_types(db)}
    errors: dict[str, str] = {}
    parsed = []
    for row in rows:
        wht = known.get(row.wht_type_id)
        if wht is None:
            continue
        company = _rate(row.company_rate, f"company_{wht.id}", errors, blank=True)
        individual = _rate(row.individual_rate, f"individual_{wht.id}", errors, blank=True)
        parsed.append((wht, company, individual, row.is_active))
    if errors:
        raise ValidationFailed(field_errors=errors)
    changes = {}
    for wht, company, individual, active in parsed:
        before = (wht.company_rate, wht.individual_rate, wht.is_active)
        wht.company_rate, wht.individual_rate, wht.is_active = company, individual, active
        if before != (company, individual, active):
            changes[wht.key] = {
                "from": [str(v) for v in before],
                "to": [str(company), str(individual), str(active)],
            }
    db.flush()
    if changes:
        record_by(db, actor, "finance.tax.wht_rates", entity_type="wht_type", changes=changes)


# ------------------------------------------------------------------ stamp duty


def stamp_types(db: Session, *, active_only: bool = False) -> list[StampDutyType]:
    query = select(StampDutyType).order_by(StampDutyType.name)
    if active_only:
        query = query.where(StampDutyType.is_active)
    return list(db.scalars(query))


def stamp_amount(duty: StampDutyType, net: Decimal) -> Decimal:
    if duty.basis == "flat":
        return duty.value.quantize(CENT, rounding=ROUND_HALF_UP)
    return (net * duty.value / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def save_stamp_type(
    db: Session,
    actor: Actor,
    stamp_id: uuid.UUID | None,
    *,
    name: str,
    basis: str,
    value: str,
    is_active: bool,
) -> uuid.UUID:
    _require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    name = name.strip()
    if not name:
        errors["stamp_name"] = "Name the stamp duty type."
    else:
        clash = select(StampDutyType.id).where(func.lower(StampDutyType.name) == name.lower())
        if stamp_id:
            clash = clash.where(StampDutyType.id != stamp_id)
        if db.scalar(clash):
            errors["stamp_name"] = "A stamp duty type already has that name."
    if basis not in STAMP_BASES:
        errors["basis"] = "Choose a flat amount or a percentage."
    amount: Decimal | None = None
    if basis == "percent":
        amount = _rate(value, "value", errors)
    else:
        try:
            amount = Decimal(value.strip().replace(",", "").replace("₦", ""))
            if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
                raise InvalidOperation
        except InvalidOperation:
            errors["value"] = "Enter an amount in naira and kobo."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(StampDutyType, stamp_id) if stamp_id else None
    if row is None:
        row = StampDutyType(name=name, basis=basis, value=amount)
        db.add(row)
    row.name, row.basis, row.value, row.is_active = name, basis, amount, is_active  # type: ignore[assignment]
    db.flush()
    record_by(
        db,
        actor,
        "finance.tax.stamp_type",
        entity_type="stamp_duty_type",
        entity_id=str(row.id),
        changes={"name": name, "basis": basis, "value": str(amount), "active": is_active},
    )
    return row.id


# ------------------------------------------------------------------ receivables settings


@dataclass(frozen=True, slots=True)
class SettingsInput:
    receivables_code: str
    vat_output_code: str
    vat_input_code: str
    wht_receivable_code: str
    wht_payable_code: str
    stamp_duty_code: str
    default_terms_days: str
    remind_before_days: str
    remind_on_due: bool
    remind_every_days: str
    return_due_day: str
    payables_code: str = ""  # blank keeps the current one
    price_tolerance: str = ""


# The account each setting needs, and its type.
SETTING_ACCOUNTS = (
    ("receivables_code", "Trade receivables", "asset"),
    ("payables_code", "Trade payables", "liability"),
    ("vat_output_code", "VAT output", "liability"),
    ("vat_input_code", "VAT input", "asset"),
    ("wht_receivable_code", "WHT receivable", "asset"),
    ("wht_payable_code", "WHT payable", "liability"),
    ("stamp_duty_code", "Stamp duty payable", "liability"),
)


def _whole(value: str, key: str, low: int, high: int, errors: dict[str, str]) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        number = -1
    if not low <= number <= high:
        errors[key] = f"Enter a whole number from {low} to {high}."
    return number


def save_settings(db: Session, actor: Actor, data: SettingsInput) -> None:
    _require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    row = periods.settings(db)
    codes = {key: getattr(data, key).strip() or getattr(row, key) for key, _, _ in SETTING_ACCOUNTS}
    found = accounts.by_codes(db, set(codes.values()))
    for key, label, kind in SETTING_ACCOUNTS:
        account = found.get(codes[key])
        if account is None or account.is_header or not account.is_active:
            errors[key] = f"Choose an active {kind} account for {label}."
        elif account.type != kind:
            errors[key] = f"{label} must be an {kind} account."
    terms = _whole(data.default_terms_days, "default_terms_days", 0, MAX_TERMS, errors)
    before_days = _whole(data.remind_before_days, "remind_before_days", 0, MAX_DAYS, errors)
    every = _whole(data.remind_every_days, "remind_every_days", 1, MAX_DAYS, errors)
    due_day = _whole(data.return_due_day, "return_due_day", 1, 28, errors)
    tolerance = (
        _rate(data.price_tolerance, "price_tolerance", errors)
        if data.price_tolerance.strip()
        else row.price_tolerance
    )
    if errors:
        raise ValidationFailed(field_errors=errors)
    keys = (*codes, "default_terms_days", "return_due_day", "price_tolerance")
    before = {k: getattr(row, k) for k in keys}
    for key, code in codes.items():
        setattr(row, key, code)
    row.default_terms_days, row.remind_before_days = terms, before_days
    row.remind_on_due, row.remind_every_days = data.remind_on_due, every
    row.return_due_day, row.price_tolerance = due_day, tolerance  # type: ignore[assignment]
    db.flush()
    after = {k: getattr(row, k) for k in before}
    record_by(
        db,
        actor,
        "finance.tax.settings",
        entity_type="finance_settings",
        changes={k: {"from": before[k], "to": v} for k, v in after.items() if before[k] != v},
    )


# ------------------------------------------------------------------ returns


@dataclass(frozen=True, slots=True)
class ReturnLine:
    journal_id: uuid.UUID
    number: str
    journal_date: date
    memo: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ReturnRow:
    tax: str
    period_start: date
    due: date
    status: str  # draft, filed or paid
    overdue: bool
    output: Decimal  # as filed, or live while a draft
    input: Decimal
    changed_by: Decimal | None  # live net minus filed net, when the ledger moved since
    filed_on: date | None = None
    filing_reference: str | None = None
    paid_on: date | None = None
    payment_reference: str | None = None

    @property
    def net(self) -> Decimal:
        return self.output - self.input

    @property
    def label(self) -> str:
        return f"{periods.MONTHS[self.period_start.month - 1]} {self.period_start.year}"

    @property
    def key(self) -> str:
        return self.period_start.strftime("%Y-%m")


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _codes(row: FinanceSettings, tax: str) -> tuple[str, str | None]:
    """(account whose credits are owed, account whose debits are reclaimed)."""
    if tax == "vat":
        return row.vat_output_code, row.vat_input_code
    if tax == "wht":
        return row.wht_payable_code, None
    return row.stamp_duty_code, None


def _lines(db: Session, code: str, start: date, *, credit_side: bool) -> list[ReturnLine]:
    end = _next_month(start)
    signed = (
        JournalLine.credit - JournalLine.debit
        if credit_side
        else (JournalLine.debit - JournalLine.credit)
    )
    rows = db.execute(
        select(Journal.id, Journal.number, Journal.journal_date, Journal.memo, func.sum(signed))
        .join(JournalLine, JournalLine.journal_id == Journal.id)
        .where(
            Journal.status == "posted",
            Journal.source != SETTLEMENT_SOURCE,
            Journal.journal_date >= start,
            Journal.journal_date < end,
            JournalLine.account_code == code,
        )
        .group_by(Journal.id)
        .order_by(Journal.journal_date, Journal.number)
    ).all()
    return [
        ReturnLine(jid, number or "", day, memo, amount) for jid, number, day, memo, amount in rows
    ]


def _live(db: Session, tax: str, start: date) -> tuple[list[ReturnLine], list[ReturnLine]]:
    out_code, in_code = _codes(periods.settings(db), tax)
    outputs = _lines(db, out_code, start, credit_side=True)
    inputs = _lines(db, in_code, start, credit_side=False) if in_code else []
    return outputs, inputs


def _total(lines: list[ReturnLine]) -> Decimal:
    return sum((line.amount for line in lines), ZERO)


def due_date(db: Session, start: date) -> date:
    return _next_month(start).replace(day=periods.settings(db).return_due_day)


def _row(db: Session, tax: str, start: date, saved: TaxReturn | None, today: date) -> ReturnRow:
    outputs, inputs = _live(db, tax, start)
    live_out, live_in = _total(outputs), _total(inputs)
    due = due_date(db, start)
    if saved is None:
        return ReturnRow(tax, start, due, "draft", today > due, live_out, live_in, None)
    filed_out, filed_in = saved.output_amount or ZERO, saved.input_amount or ZERO
    moved = (live_out - live_in) - (filed_out - filed_in)
    return ReturnRow(
        tax,
        start,
        due,
        saved.status,
        False,
        filed_out,
        filed_in,
        moved or None,
        saved.filed_on,
        saved.filing_reference,
        saved.paid_on,
        saved.payment_reference,
    )


def _check_tax(tax: str) -> None:
    if tax not in TAXES:
        raise ValidationFailed(field_errors={"tax": "Choose VAT, withholding tax or stamp duty."})


def returns(db: Session, actor: Actor, tax: str, *, today: date) -> list[ReturnRow]:
    """Every ended month from the first financial year, newest first."""
    _require(db, actor, READ)
    _check_tax(tax)
    first = db.scalar(select(func.min(FiscalYear.start_date)))
    if first is None:
        return []
    saved = {r.period_start: r for r in db.scalars(select(TaxReturn).where(TaxReturn.tax == tax))}
    months = []
    month = first.replace(day=1)
    this_month = today.replace(day=1)
    while month < this_month:
        months.append(month)
        month = _next_month(month)
    return [_row(db, tax, m, saved.get(m), today) for m in reversed(months)]


def return_detail(
    db: Session, actor: Actor, tax: str, start: date, *, today: date
) -> tuple[ReturnRow, list[ReturnLine], list[ReturnLine]]:
    _require(db, actor, READ)
    _check_tax(tax)
    start = start.replace(day=1)
    saved = db.scalars(
        select(TaxReturn).where(TaxReturn.tax == tax, TaxReturn.period_start == start)
    ).first()
    outputs, inputs = _live(db, tax, start)
    return _row(db, tax, start, saved, today), outputs, inputs


def _reference_errors(day: date | None, reference: str, key: str) -> dict[str, str]:
    errors = {}
    if day is None:
        errors[f"{key}_on"] = "Enter the date."
    if not reference.strip():
        errors[f"{key}_reference"] = "Enter the reference."
    return errors


def file_return(
    db: Session,
    actor: Actor,
    tax: str,
    start: date,
    *,
    filed_on: date | None,
    reference: str,
    today: date,
) -> None:
    _require(db, actor, MANAGE)
    _check_tax(tax)
    start = start.replace(day=1)
    if start >= today.replace(day=1):
        raise Conflict("A return can be filed once its month has ended.")
    if db.scalar(select(TaxReturn.id).where(TaxReturn.tax == tax, TaxReturn.period_start == start)):
        raise Conflict("This return has already been filed.")
    errors = _reference_errors(filed_on, reference, "filed")
    if errors:
        raise ValidationFailed(field_errors=errors)
    outputs, inputs = _live(db, tax, start)
    row = TaxReturn(
        tax=tax,
        period_start=start,
        status="filed",
        output_amount=_total(outputs),
        input_amount=_total(inputs),
        filed_on=filed_on,
        filing_reference=reference.strip()[:80],
        filed_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "finance.tax.return_filed",
        entity_type="tax_return",
        entity_id=str(row.id),
        changes={
            "period": f"{tax} {start:%Y-%m}",
            "net": str(row.output_amount - row.input_amount),  # type: ignore[operator]
        },
    )


def mark_paid(
    db: Session, actor: Actor, tax: str, start: date, *, paid_on: date | None, reference: str
) -> None:
    _require(db, actor, MANAGE)
    _check_tax(tax)
    row = db.scalars(
        select(TaxReturn)
        .where(TaxReturn.tax == tax, TaxReturn.period_start == start.replace(day=1))
        .with_for_update()
    ).first()
    if row is None:
        raise Conflict("File the return before marking it paid.")
    if row.status == "paid":
        raise Conflict("This return is already marked paid.")
    errors = _reference_errors(paid_on, reference, "paid")
    if errors:
        raise ValidationFailed(field_errors=errors)
    row.status, row.paid_on = "paid", paid_on
    row.payment_reference, row.paid_by = reference.strip()[:80], actor.user_id
    db.flush()
    record_by(
        db,
        actor,
        "finance.tax.return_paid",
        entity_type="tax_return",
        entity_id=str(row.id),
        changes={"status": {"from": "filed", "to": "paid"}},
    )
