"""The payroll calculation on its own (ADR 0021). Figures are worked by hand in the
comments, using the Nigeria Tax Act 2025 bands seeded for 2026."""

from decimal import Decimal

import pytest

from app.modules.payroll import calc

D = Decimal
BANDS: list[tuple[Decimal | None, Decimal]] = [
    (D(800000), D("0")),
    (D(2200000), D("0.15")),
    (D(9000000), D("0.18")),
    (D(13000000), D("0.21")),
    (D(25000000), D("0.23")),
    (None, D("0.25")),
]
RATES = calc.Rates(
    bands=BANDS,
    rent_relief_rate=D("0.20"),
    rent_relief_cap=D(500000),
    pension_employee_rate=D("0.08"),
    pension_employer_rate=D("0.10"),
    nhf_rate=D("0.025"),
    nhis_rate=D(0),
    nsitf_rate=D("0.01"),
    minimum_wage=D(70000),
    overtime_multiplier=D("1.5"),
    hours_per_month=173,
)
BASIC = calc.Component("basic", "Basic salary", "earning", "fixed", None, None, True, True)
HOUSING = calc.Component("housing", "Housing allowance", "earning", "fixed", None, None, True, True)
TRANSPORT = calc.Component(
    "transport", "Transport allowance", "earning", "fixed", None, None, True, True
)
STANDARD = [BASIC, HOUSING, TRANSPORT]
PAY = {"basic": D(200000), "housing": D(100000), "transport": D(50000)}


def _run(**changes: object) -> calc.Result:
    values: dict[str, object] = {
        "month": 1,
        "days_in_month": 30,
        "days_paid": 30,
        "components": STANDARD,
        "amounts": PAY,
    }
    values.update(changes)
    return calc.calculate(calc.Inputs(**values), RATES)  # type: ignore[arg-type]


def test_annual_tax_through_every_band() -> None:
    # 0 + 330,000 + 1,620,000 + 2,730,000 + 5,750,000 + 10,000,000 x 25%
    assert calc.annual_tax(D(60000000), BANDS) == D(12930000)
    assert calc.annual_tax(D(800000), BANDS) == 0
    assert calc.annual_tax(D(-5), BANDS) == 0


def test_a_standard_month() -> None:
    r = _run()
    # Gross 350,000; pension 8% of 350,000 = 28,000; NHF 2.5% of 200,000 = 5,000.
    assert (r.gross, r.pension_employee, r.nhf) == (D(350000), D(28000), D(5000))
    # Taxable 317,000. January's bands are a twelfth: 66,666.67 at 0%, 183,333.33 at
    # 15% (27,500), and 67,000 at 18% (12,060).
    assert r.taxable == D(317000)
    assert r.paye == D("39560.00")
    assert r.net == D(350000) - D("39560.00") - D(28000) - D(5000)
    assert (r.pension_employer, r.nsitf) == (D(35000), D(3500))
    assert r.warnings == []
    assert [line.code for line in r.lines] == [
        "basic",
        "housing",
        "transport",
        "paye",
        "pension",
        "nhf",
    ]


def test_paye_is_cumulative_through_the_year() -> None:
    february = _run(month=2, taxable_before=D(317000), paye_before=D("39560.00"))
    assert february.paye == D("39560.00")
    assert (february.ytd_taxable, february.ytd_paye) == (D(634000), D("79120.00"))
    # January's PAYE was never deducted: February catches up.
    catch_up = _run(month=2, taxable_before=D(317000), paye_before=D(0))
    assert catch_up.paye == D("79120.00")
    # Over-deducted earlier: nothing this month, never a refund.
    assert _run(month=2, taxable_before=D(317000), paye_before=D(200000)).paye == 0


def test_rent_relief_reduces_tax_and_is_capped() -> None:
    # 20% of 1,800,000 = 360,000 a year, 30,000 a month: tax on 287,000 is
    # 27,500 + 37,000 x 18% = 34,160.
    r = _run(annual_rent=D(1800000))
    assert (r.rent_relief, r.paye) == (D(30000), D("34160.00"))
    capped = _run(annual_rent=D(5000000))  # 20% would be 1,000,000; the cap is 500,000
    assert capped.rent_relief == D("41666.67")


def test_joiners_are_prorated_by_calendar_days() -> None:
    r = _run(days_paid=15)
    assert r.gross == D(175000)
    basic = next(line for line in r.lines if line.code == "basic")
    assert (basic.amount, basic.detail) == (D(100000), "15 of 30 days")
    assert not r.warnings  # the minimum wage is judged on the full month


def test_percentage_components_follow_their_base() -> None:
    meal = calc.Component(
        "meal", "Meal allowance", "earning", "percent", D("0.1"), "basic", True, False
    )
    r = _run(components=[*STANDARD, meal], amounts={**PAY, "meal": D(0)}, days_paid=15)
    line = next(line for line in r.lines if line.code == "meal")
    assert (line.amount, line.detail) == (D(10000), "10% of Basic salary")
    assert r.pension_employee == D(14000)  # meal isn't pensionable


def test_overtime_is_paid_at_the_multiplier() -> None:
    # 173,000 basic over 173 hours is 1,000 an hour; 6 hours at 1.5x.
    r = _run(amounts={"basic": D(173000)}, components=[BASIC], overtime_minutes=360)
    line = next(line for line in r.lines if line.code == "overtime")
    assert (
        line.amount == D(9000) and line.detail is not None and line.detail.startswith("6 h at 1.5")
    )
    assert r.pension_employee == D(13840)  # overtime isn't pensionable


@pytest.mark.parametrize(
    ("amounts", "deductions", "warning"),
    [
        ({"basic": D(60000)}, [], "below the ₦70,000.00 minimum wage"),
        ({"basic": D(100000)}, [("loan", "Loan", D(200000))], "net pay is negative"),
        ({"housing": D(100000)}, [], "No basic salary"),
    ],
)
def test_warnings(
    amounts: dict[str, Decimal], deductions: list[tuple[str, str, Decimal]], warning: str
) -> None:
    r = _run(components=STANDARD, amounts=amounts, deductions=deductions)
    assert any(warning in w for w in r.warnings)


def test_recurring_deductions_come_off_net_not_tax() -> None:
    plain = _run()
    r = _run(deductions=[("cooperative", "Cooperative", D("6250.50"))])
    assert r.paye == plain.paye and r.net == plain.net - D("6250.50")
    assert r.other_deductions == D("6250.50")
