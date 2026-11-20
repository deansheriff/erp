"""The payroll calculation (ADR 0021), free of the database so it can be tested on its
own: one person, one month."""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
MONTHS = 12
TIMES = chr(0xD7)  # the multiplication sign


def kobo(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Rates:
    bands: list[tuple[Decimal | None, Decimal]]  # (annual width or None for the rest, rate)
    rent_relief_rate: Decimal
    rent_relief_cap: Decimal
    pension_employee_rate: Decimal
    pension_employer_rate: Decimal
    nhf_rate: Decimal
    nhis_rate: Decimal
    nsitf_rate: Decimal
    minimum_wage: Decimal
    overtime_multiplier: Decimal
    hours_per_month: int


@dataclass(frozen=True, slots=True)
class Component:
    code: str
    name: str
    kind: str  # earning or deduction
    calculation: str  # fixed or percent
    percent: Decimal | None
    base_code: str | None
    taxable: bool
    pensionable: bool


@dataclass(frozen=True, slots=True)
class Inputs:
    month: int  # 1 to 12: months of the tax year so far, this one included
    days_in_month: int
    days_paid: int
    components: list[Component]  # those on the person's pay, in display order
    amounts: dict[str, Decimal]  # monthly amounts of fixed components, by code
    overtime_minutes: int = 0
    deductions: list[tuple[str, str, Decimal]] = field(default_factory=list)  # (code, name, amount)
    annual_rent: Decimal | None = None  # verified declaration for the tax year
    taxable_before: Decimal = ZERO  # earlier months of the tax year
    paye_before: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Line:
    code: str
    name: str
    kind: str
    amount: Decimal
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    lines: list[Line]
    gross: Decimal
    taxable: Decimal
    paye: Decimal
    pension_employee: Decimal
    pension_employer: Decimal
    nhf: Decimal
    nhis: Decimal
    nsitf: Decimal
    other_deductions: Decimal
    net: Decimal
    rent_relief: Decimal
    ytd_taxable: Decimal
    ytd_paye: Decimal
    warnings: list[str]


def annual_tax(income: Decimal, bands: list[tuple[Decimal | None, Decimal]]) -> Decimal:
    """Tax on an amount through progressive bands (each band taxes the next `width`)."""
    left, tax = max(income, ZERO), ZERO
    for width, rate in bands:
        portion = left if width is None else min(left, width)
        tax += portion * rate
        left -= portion
        if left <= 0:
            break
    return tax


def cumulative_paye(
    *,
    ytd_taxable: Decimal,
    ytd_relief: Decimal,
    months: int,
    bands: list[tuple[Decimal | None, Decimal]],
    paid_before: Decimal,
) -> Decimal:
    """This month's PAYE: tax on taxable pay so far, through the annual bands scaled to
    the months elapsed, less what has already been deducted. Never negative."""
    scale = Decimal(months) / MONTHS
    scaled = [(None if width is None else width * scale, rate) for width, rate in bands]
    due = kobo(annual_tax(ytd_taxable - ytd_relief, scaled))
    return max(due - paid_before, ZERO)


def rent_relief_annual(annual_rent: Decimal | None, rates: Rates) -> Decimal:
    if not annual_rent:
        return ZERO
    return min(annual_rent * rates.rent_relief_rate, rates.rent_relief_cap)


def _fmt(value: Decimal) -> str:
    return f"{value.normalize():f}"


@dataclass(frozen=True, slots=True)
class _Earnings:
    lines: list[Line]
    prorated: dict[str, Decimal]
    taxable: Decimal
    pensionable: Decimal
    full_month_gross: Decimal  # before proration, for the minimum wage check


def _earnings(inputs: Inputs) -> _Earnings:
    """Fixed earnings prorated by days paid, then percentages of them."""
    factor = Decimal(inputs.days_paid) / Decimal(inputs.days_in_month)
    by_code = {c.code: c for c in inputs.components}
    prorated: dict[str, Decimal] = {}
    full_month = ZERO
    detail = (
        None
        if inputs.days_paid == inputs.days_in_month
        else f"{inputs.days_paid} of {inputs.days_in_month} days"
    )
    for c in inputs.components:
        if c.kind == "earning" and c.calculation == "fixed":
            monthly = inputs.amounts.get(c.code, ZERO)
            full_month += monthly
            prorated[c.code] = kobo(monthly * factor)
    for c in inputs.components:
        if c.kind == "earning" and c.calculation == "percent" and c.percent and c.base_code:
            full_month += inputs.amounts.get(c.base_code, ZERO) * c.percent
            prorated[c.code] = kobo(prorated.get(c.base_code, ZERO) * c.percent)
    earnings = [c for c in inputs.components if c.kind == "earning" and c.code in prorated]
    lines = []
    for c in earnings:
        if prorated[c.code]:
            extra = (
                f"{_fmt(c.percent * 100)}% of {by_code[c.base_code].name}"
                if c.calculation == "percent" and c.percent and c.base_code in by_code
                else detail
            )
            lines.append(Line(c.code, c.name, "earning", prorated[c.code], extra))
    return _Earnings(
        lines=lines,
        prorated=prorated,
        taxable=sum((prorated[c.code] for c in earnings if c.taxable), ZERO),
        pensionable=sum((prorated[c.code] for c in earnings if c.pensionable), ZERO),
        full_month_gross=full_month,
    )


def _overtime(inputs: Inputs, rates: Rates) -> Line | None:
    """Approved overtime at the multiplier of hourly basic (monthly basic over the
    month's hours)."""
    if not inputs.overtime_minutes or not inputs.amounts.get("basic"):
        return None
    hourly = inputs.amounts["basic"] / rates.hours_per_month
    hours = Decimal(inputs.overtime_minutes) / 60
    amount = kobo(hourly * hours * rates.overtime_multiplier)
    if not amount:
        return None
    detail = f"{_fmt(hours.quantize(CENT))} h at {_fmt(rates.overtime_multiplier)}{TIMES}"
    return Line("overtime", "Overtime", "earning", amount, detail)


def calculate(inputs: Inputs, rates: Rates) -> Result:
    found = _earnings(inputs)
    lines = list(found.lines)
    warnings: list[str] = []
    prorated = found.prorated
    taxable_earnings = found.taxable
    pensionable = found.pensionable
    full_month_gross = found.full_month_gross
    basic = prorated.get("basic", ZERO)
    if not inputs.amounts.get("basic"):
        warnings.append("No basic salary on this person's pay.")
    overtime = _overtime(inputs, rates)
    if overtime is not None:
        lines.append(overtime)
        taxable_earnings += overtime.amount

    gross = sum((line.amount for line in lines if line.kind == "earning"), ZERO)
    pension_employee = kobo(pensionable * rates.pension_employee_rate)
    pension_employer = kobo(pensionable * rates.pension_employer_rate)
    nhf = kobo(basic * rates.nhf_rate)
    nhis = kobo(basic * rates.nhis_rate)
    nsitf = kobo(gross * rates.nsitf_rate)
    taxable = max(taxable_earnings - pension_employee - nhf - nhis, ZERO)

    relief_year = rent_relief_annual(inputs.annual_rent, rates)
    ytd_taxable = inputs.taxable_before + taxable
    ytd_relief = kobo(relief_year * inputs.month / MONTHS)
    paye = cumulative_paye(
        ytd_taxable=ytd_taxable,
        ytd_relief=ytd_relief,
        months=inputs.month,
        bands=rates.bands,
        paid_before=inputs.paye_before,
    )
    statutory = [
        Line("paye", "PAYE", "deduction", paye),
        Line(
            "pension",
            "Pension (employee)",
            "deduction",
            pension_employee,
            f"{_fmt(rates.pension_employee_rate * 100)}%",
        ),
        Line("nhf", "NHF", "deduction", nhf, f"{_fmt(rates.nhf_rate * 100)}% of basic"),
        Line("nhis", "NHIS", "deduction", nhis),
    ]
    lines.extend(line for line in statutory if line.amount)
    other = ZERO
    for code, name, amount in inputs.deductions:
        lines.append(Line(code, name, "deduction", kobo(amount)))
        other += kobo(amount)

    net = gross - paye - pension_employee - nhf - nhis - other
    if full_month_gross < rates.minimum_wage:
        warnings.append(
            f"Monthly pay of {_naira(full_month_gross)} is below the "
            f"{_naira(rates.minimum_wage)} minimum wage."
        )
    if net < 0:
        warnings.append("Deductions are more than the pay: net pay is negative.")
    return Result(
        lines=lines,
        gross=gross,
        taxable=taxable,
        paye=paye,
        pension_employee=pension_employee,
        pension_employer=pension_employer,
        nhf=nhf,
        nhis=nhis,
        nsitf=nsitf,
        other_deductions=other,
        net=net,
        rent_relief=kobo(relief_year / MONTHS),
        ytd_taxable=ytd_taxable,
        ytd_paye=inputs.paye_before + paye,
        warnings=warnings,
    )


def _naira(amount: Decimal) -> str:
    return f"₦{kobo(amount):,.2f}"
