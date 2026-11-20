"""Effective-dated statutory settings (ADR 0021): PAYE bands and rates. The set in force on
the last day of a period applies to it."""

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.payroll.calc import Rates
from app.modules.payroll.models import PayrollRun, StatutorySet, TaxBand

MANAGE = "payroll.settings.manage"
PERCENT_FIELDS = (
    "rent_relief_rate",
    "pension_employee_rate",
    "pension_employer_rate",
    "nhf_rate",
    "nhis_rate",
    "nsitf_rate",
    "itf_rate",
)


def in_force(db: Session, on: date) -> StatutorySet | None:
    return db.scalars(
        select(StatutorySet)
        .where(StatutorySet.effective_from <= on)
        .order_by(StatutorySet.effective_from.desc())
        .limit(1)
    ).first()


def bands_of(db: Session, statutory: StatutorySet) -> list[TaxBand]:
    return list(
        db.scalars(select(TaxBand).where(TaxBand.set_id == statutory.id).order_by(TaxBand.position))
    )


def rates_of(db: Session, statutory: StatutorySet) -> Rates:
    return Rates(
        bands=[(b.width, b.rate) for b in bands_of(db, statutory)],
        rent_relief_rate=statutory.rent_relief_rate,
        rent_relief_cap=statutory.rent_relief_cap,
        pension_employee_rate=statutory.pension_employee_rate,
        pension_employer_rate=statutory.pension_employer_rate,
        nhf_rate=statutory.nhf_rate,
        nhis_rate=statutory.nhis_rate,
        nsitf_rate=statutory.nsitf_rate,
        minimum_wage=statutory.minimum_wage,
        overtime_multiplier=statutory.overtime_multiplier,
        hours_per_month=statutory.hours_per_month,
    )


def all_sets(db: Session) -> list[StatutorySet]:
    return list(db.scalars(select(StatutorySet).order_by(StatutorySet.effective_from.desc())))


def _number(value: str, field: str, *, low: Decimal, high: Decimal) -> Decimal:
    text = value.strip().replace(",", "").replace("%", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValidationFailed(field_errors={field: "Enter a number."}) from None
    if not number.is_finite() or not low <= number <= high:
        raise ValidationFailed(field_errors={field: f"Enter a number from {low} to {high}."})
    return number


def _values(s: StatutorySet, bands: list[TaxBand]) -> dict[str, object]:
    values: dict[str, object] = {
        f: str(getattr(s, f))
        for f in (
            *PERCENT_FIELDS,
            "rent_relief_cap",
            "minimum_wage",
            "overtime_multiplier",
            "hours_per_month",
            "label",
            "effective_from",
        )
    }
    values["bands"] = "; ".join(f"{b.width or 'rest'} @ {b.rate}" for b in bands)
    return values


def _parse_rates(rates: dict[str, str], errors: dict[str, str]) -> dict[str, Decimal]:
    """Percent fields as fractions (8 gives 0.08) and the other numbers as entered."""
    parsed: dict[str, Decimal] = {}
    for name in PERCENT_FIELDS:
        try:
            parsed[name] = (
                _number(rates.get(name, ""), name, low=Decimal(0), high=Decimal(100)) / 100
            )
        except ValidationFailed as exc:
            errors.update(exc.field_errors or {})
    for name, low, high in (
        ("rent_relief_cap", Decimal(0), Decimal("1000000000")),
        ("minimum_wage", Decimal(0), Decimal("1000000000")),
        ("overtime_multiplier", Decimal(0), Decimal(10)),
        ("hours_per_month", Decimal(1), Decimal(744)),
    ):
        try:
            parsed[name] = _number(rates.get(name, ""), name, low=low, high=high)
        except ValidationFailed as exc:
            errors.update(exc.field_errors or {})
    return parsed


def _parse_bands(
    bands: list[tuple[str, str]], errors: dict[str, str]
) -> list[tuple[Decimal | None, Decimal]]:
    """(width, rate) rows; only the top band has no width."""
    clean: list[tuple[Decimal | None, Decimal]] = []
    rows = [(w.strip(), r.strip()) for w, r in bands if w.strip() or r.strip()]
    for i, (width, rate) in enumerate(rows):
        last = i == len(rows) - 1
        try:
            if last and width:
                raise ValidationFailed(
                    field_errors={f"band_{i}": "Leave the top band's width blank."}
                )
            if not last and not width:
                raise ValidationFailed(field_errors={f"band_{i}": "Enter the band's width."})
            w = (
                _number(width, f"band_{i}", low=Decimal("0.01"), high=Decimal("1e12"))
                if width
                else None
            )
            clean.append((w, _number(rate, f"band_{i}", low=Decimal(0), high=Decimal(100)) / 100))
        except ValidationFailed as exc:
            errors.update(exc.field_errors or {})
    if not rows:
        errors["band_0"] = "Enter at least one tax band."
    return clean


def save_set(
    db: Session,
    actor: Actor,
    set_id: uuid.UUID | None,
    *,
    effective_from: date | None,
    label: str,
    rates: dict[str, str],
    bands: list[tuple[str, str]],
) -> uuid.UUID:
    """Create or change a set. Percentages are entered as percents (8 means 8%); bands
    are (width, percent) with a blank width for the top band. A set already used by an
    approved run can't be changed: add a new one from a later date."""
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()
    if effective_from is None:
        raise ValidationFailed(field_errors={"effective_from": "Choose the date it applies from."})
    errors: dict[str, str] = {}
    parsed = _parse_rates(rates, errors)
    clean_bands = _parse_bands(bands, errors)
    if errors:
        raise ValidationFailed(field_errors=errors)
    clash = db.scalar(
        select(StatutorySet.id).where(
            StatutorySet.effective_from == effective_from,
            *([StatutorySet.id != set_id] if set_id else []),
        )
    )
    if clash:
        raise ValidationFailed(field_errors={"effective_from": "Another set starts on this date."})
    if set_id is None:
        row = StatutorySet(
            effective_from=effective_from, label=label.strip()[:120] or "Statutory rates"
        )
        before: dict[str, object] = {}
    else:
        found = db.get(StatutorySet, set_id)
        if found is None:
            raise NotFound()
        if db.scalar(
            select(PayrollRun.id).where(
                PayrollRun.statutory_set_id == found.id,
                PayrollRun.status.in_(("approved", "posted", "paid")),
            )
        ):
            raise Conflict(
                "Approved runs used these rates. Add a new set from a later date instead."
            )
        row = found
        before = _values(row, bands_of(db, row))
        row.effective_from, row.label = effective_from, label.strip()[:120] or row.label
    for name, value in parsed.items():
        setattr(row, name, int(value) if name == "hours_per_month" else value)
    db.add(row)
    db.flush()
    for band in bands_of(db, row):
        db.delete(band)
    db.flush()
    for position, (width, rate) in enumerate(clean_bands, start=1):
        db.add(TaxBand(set_id=row.id, position=position, width=width, rate=rate))
    db.flush()
    record_by(
        db,
        actor,
        "payroll.statutory.save",
        entity_type="statutory_set",
        entity_id=str(row.id),
        changes=diff_changes(before, _values(row, bands_of(db, row))),
    )
    return row.id
