"""Phase 10e: asset categories and the register, assets from bills, monthly depreciation
runs posted by a second person, and revaluations and disposals (ADR 0027)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.modules.finance import assets, bills, depreciation, journals, suppliers
from app.modules.finance.assets import AssetInput, CategoryInput
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.models import (
    AssetCategory,
    BankAccount,
    DepreciationRun,
    FixedAsset,
    JournalLine,
)
from tests.finance.conftest import Finance

pytestmark = pytest.mark.db

D = Decimal


def _category(f: Finance, name: str) -> AssetCategory:
    return f.db.scalars(select(AssetCategory).where(AssetCategory.name == name)).one()


def _asset(
    f: Finance, category: str, cost: str, acquired: date, *, life: str = "", **extra: str
) -> uuid.UUID:
    c = _category(f, category)
    asset_id, _ = assets.register(
        f.db,
        f.hr.actor("Chi"),
        None,
        AssetInput(
            name=extra.pop("name", f"{category} item"),
            category_id=str(c.id),
            acquired_on=acquired,
            cost=cost,
            life_months=life or str(c.life_months or ""),
            **extra,
        ),
    )
    return asset_id


def _lines(f: Finance, journal_id: uuid.UUID | None) -> dict[str, tuple[Decimal, Decimal]]:
    assert journal_id is not None
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for jl in f.db.scalars(select(JournalLine).where(JournalLine.journal_id == journal_id)):
        debit, credit = out.get(jl.account_code, (D(0), D(0)))
        out[jl.account_code] = (debit + jl.debit, credit + jl.credit)
    return out


def _run(f: Finance, today: date, preparer: str = "Ada", poster: str = "Bola") -> DepreciationRun:
    run = depreciation.prepare(f.db, f.hr.actor(preparer), today=today)
    assert run is not None
    depreciation.post(f.db, f.hr.actor(poster), run.id)
    return run


def test_straight_line_runs_month_by_month_and_catch_up(finance: Finance) -> None:
    f, a = finance, finance.hr.actor
    laptop = _asset(f, "Computer equipment", "360,000", date(2026, 1, 15))
    assert depreciation.next_month(f.db) == date(2026, 1, 1)
    run = depreciation.prepare(f.db, a("Ada"), today=date(2026, 3, 5))
    assert run is not None and run.month == date(2026, 1, 1) and run.total == D("10000.00")
    with pytest.raises(PermissionDenied):  # Ada prepared it
        depreciation.post(f.db, a("Ada"), run.id)
    journal = depreciation.post(f.db, a("Bola"), run.id)
    assert journal is not None
    assert _lines(f, journal.id) == {"6500": (D("10000.00"), 0), "1551": (0, D("10000.00"))}
    with pytest.raises(Conflict):  # asset journals are corrected through the asset
        journals.reverse(f.db, a("Ada"), journal.id, on=date(2026, 1, 31))

    # Registered late: February's run catches up its January too.
    chair = _asset(f, "Furniture and fittings", "600,000", date(2026, 1, 20))
    run = depreciation.prepare(f.db, a("Ada"), today=date(2026, 3, 5))
    assert run is not None and run.month == date(2026, 2, 1)
    lines = {x.asset_id: (x.months, x.amount) for x in depreciation._lines(f.db, run.id)}
    assert lines == {laptop: (1, D("10000.00")), chair: (2, D("20000.00"))}

    # Anything registered after preparing makes it stale until recalculated.
    _asset(f, "Computer equipment", "180,000", date(2026, 2, 1))
    with pytest.raises(Conflict, match="Recalculate"):
        depreciation.post(f.db, a("Bola"), run.id)
    run = depreciation.prepare(f.db, a("Chi"), today=date(2026, 3, 5))
    assert run is not None and run.total == D("35000.00")
    depreciation.post(f.db, a("Bola"), run.id)
    row = f.db.get(FixedAsset, laptop)
    assert row is not None and row.accumulated == D("20000.00") and row.months_left == 34
    assert row.depreciated_to == date(2026, 2, 1)

    # The nightly task drafts March once it has ended; anyone allowed posts its run.
    assert depreciation.draft_due(f.db, today=date(2026, 3, 31)) is False
    assert depreciation.draft_due(f.db, today=date(2026, 4, 1)) is True
    task_run = depreciation.draft(f.db)
    assert task_run is not None and task_run.prepared_by is None
    depreciation.post(f.db, a("Ada"), task_run.id)
    assert depreciation.next_month(f.db) == date(2026, 4, 1)
    assert depreciation.prepare(f.db, a("Ada"), today=date(2026, 3, 31)) is None


def test_methods_land_on_the_residual(finance: Finance) -> None:
    f = finance
    straight = f.db.get(
        FixedAsset, _asset(f, "Computer equipment", "100,000", date(2026, 1, 1), life="3")
    )
    assert straight is not None
    charge = depreciation.simulate(straight, 5)
    assert charge.schedule == (D("33333.33"), D("33333.34"), D("33333.33"), D(0), D(0))

    cid = assets.save_category(
        f.db,
        f.hr.actor("Chi"),
        None,
        CategoryInput(
            "Generators",
            "reducing_balance",
            "",
            "25",
            str(f.account("1520").id),
            str(f.account("1521").id),
            str(f.account("6500").id),
        ),
    )
    reducing = f.db.get(
        FixedAsset,
        _asset(f, "Generators", "120,000", date(2026, 1, 1), rate="25", residual="20,000"),
    )
    assert reducing is not None and reducing.category_id == cid
    assert depreciation.simulate(reducing, 1).amount == D("2500.00")
    assert depreciation.simulate(reducing, 600).amount == D("100000.00")

    land = f.db.get(FixedAsset, _asset(f, "Land", "50,000,000", date(2026, 1, 1)))
    assert land is not None and depreciation.simulate(land, 12).amount == 0
    with pytest.raises(ValidationFailed) as exc:
        assets.save_category(
            f.db,
            f.hr.actor("Chi"),
            None,
            CategoryInput("Bad", "straight_line", "0", "", "", "", ""),
        )
    assert {"life_months", "cost_account_id"} <= set(exc.value.field_errors or {})


def test_revaluation_then_disposal(finance: Finance) -> None:
    f, a = finance, finance.hr.actor
    hilux = _asset(f, "Motor vehicles", "4,800,000", date(2026, 1, 10))
    for month in (2, 3, 4):
        _run(f, date(2026, month, 1))
    vehicle = f.db.get(FixedAsset, hilux)
    assert vehicle is not None and vehicle.accumulated == D("300000.00")

    with pytest.raises(ValidationFailed):
        assets.record_change(
            f.db,
            a("Chi"),
            hilux,
            kind="revaluation",
            on=date(2026, 4, 15),
            amount="",
            bank_id="",
            reason="",
        )
    march = assets.record_change(
        f.db,
        a("Chi"),
        hilux,
        kind="revaluation",
        on=date(2026, 3, 20),
        amount="5,000,000",
        bank_id="",
        reason="Valuer's report",
    )
    with pytest.raises(Conflict, match="already posted for March"):
        assets.approve_change(f.db, a("Ada"), march)
    assets.delete_change(f.db, a("Chi"), march)

    change = assets.record_change(
        f.db,
        a("Chi"),
        hilux,
        kind="revaluation",
        on=date(2026, 4, 15),
        amount="5,000,000",
        bank_id="",
        reason="Valuer's report",
    )
    with pytest.raises(Conflict):  # one change waits at a time
        assets.record_change(
            f.db,
            a("Chi"),
            hilux,
            kind="disposal",
            on=date(2026, 4, 16),
            amount="0",
            bank_id="",
            reason="x",
        )
    with pytest.raises(PermissionDenied):
        assets.approve_change(f.db, a("Chi"), change)
    assets.approve_change(f.db, a("Ada"), change)
    view = assets.view(f.db, a("Ada"), hilux)
    assert view.changes[0].result == D("500000.00")
    assert view.changes[0].journal is not None
    assert _lines(f, view.changes[0].journal[0]) == {
        "1531": (D("300000.00"), 0),
        "1530": (D("200000.00"), 0),
        "3300": (0, D("500000.00")),
    }
    f.db.refresh(vehicle)
    assert (vehicle.gross, vehicle.accumulated, vehicle.reserve) == (
        D("5000000.00"),
        0,
        D("500000.00"),
    )
    assert vehicle.depreciated_to == date(2026, 3, 1) and vehicle.months_left == 45

    run = _run(f, date(2026, 5, 1))  # April, on the new value over the 45 months left
    assert run.month == date(2026, 4, 1) and run.total == D("111111.11")

    bank = f.db.scalars(select(BankAccount)).one()
    with pytest.raises(ValidationFailed):  # proceeds need a bank account
        assets.record_change(
            f.db,
            a("Chi"),
            hilux,
            kind="disposal",
            on=date(2026, 5, 20),
            amount="4,000,000",
            bank_id="",
            reason="Sold",
        )
    sale = assets.record_change(
        f.db,
        a("Chi"),
        hilux,
        kind="disposal",
        on=date(2026, 5, 20),
        amount="4,000,000",
        bank_id=str(bank.id),
        reason="Sold to staff",
    )
    assets.approve_change(f.db, a("Bola"), sale)
    view = assets.view(f.db, a("Ada"), hilux)
    assert view.status == "disposed" and view.changes[0].result == D("-888888.89")
    assert view.changes[0].journal is not None
    assert _lines(f, view.changes[0].journal[0]) == {
        "1531": (D("111111.11"), 0),
        "1010": (D("4000000.00"), 0),
        "1530": (0, D("5000000.00")),
        "4910": (D("888888.89"), 0),
        "3300": (D("500000.00"), 0),
        "3200": (0, D("500000.00")),
    }
    f.db.refresh(vehicle)
    assert vehicle.status == "disposed" and vehicle.reserve == 0
    # Nothing in May: none in the month of disposal.
    assert depreciation.prepare(f.db, a("Ada"), today=date(2026, 6, 1)) is not None
    assert depreciation.compute(f.db, date(2026, 5, 1)) == {}


def test_a_decrease_uses_the_reserve_then_profit(finance: Finance) -> None:
    f, a = finance, finance.hr.actor
    land = _asset(f, "Land", "10,000,000", date(2026, 1, 5))
    for value, on in (("12,000,000", date(2026, 2, 1)), ("9,000,000", date(2026, 3, 1))):
        change = assets.record_change(
            f.db,
            a("Chi"),
            land,
            kind="revaluation",
            on=on,
            amount=value,
            bank_id="",
            reason="Valuation",
        )
        assets.approve_change(f.db, a("Ada"), change)
    view = assets.view(f.db, a("Ada"), land)
    assert view.changes[0].journal is not None
    assert _lines(f, view.changes[0].journal[0]) == {
        "1510": (0, D("3000000.00")),
        "3300": (D("2000000.00"), 0),
        "6910": (D("1000000.00"), 0),
    }
    up = assets.record_change(
        f.db,
        a("Chi"),
        land,
        kind="revaluation",
        on=date(2026, 4, 1),
        amount="11,500,000",
        bank_id="",
        reason="Valuation",
    )
    assets.approve_change(f.db, a("Ada"), up)
    view = assets.view(f.db, a("Ada"), land)
    assert view.changes[0].journal is not None
    assert _lines(f, view.changes[0].journal[0]) == {  # reverses the loss first
        "1510": (D("2500000.00"), 0),
        "6910": (0, D("1000000.00")),
        "3300": (0, D("1500000.00")),
    }


def _bill(f: Finance, lines: list[tuple[str, str, str]]) -> uuid.UUID:
    supplier = suppliers.save(
        f.db,
        f.hr.actor("Chi"),
        None,
        suppliers.SupplierInput(
            "Lagos Office Supplies Ltd", "company", "", "", "", "", "30", "", "", "", "", "", True
        ),
    )
    bill = bills.save_bill(
        f.db,
        f.hr.actor("Chi"),
        None,
        BillInput(
            "bill",
            str(supplier),
            "LOS/881",
            date(2026, 2, 10),
            None,
            "",
            "",
            [
                BillLineInput("", text, str(f.account(code).id), "", "1", price, "exempt")
                for text, code, price in lines
            ],
        ),
    )
    assert bills.approve(f.db, f.hr.actor("Ada"), bill) == "posted"
    return bill


def test_assets_from_bills_opening_balances_and_the_ledger_check(finance: Finance) -> None:
    f, a = finance, finance.hr.actor
    _bill(
        f,
        [
            ("Dell Latitude laptops", "1550", "1,850,000"),
            ("Mouse", "1550", "15,000"),  # below the threshold
            ("Toner", "6300", "250,000"),  # an expense
        ],
    )
    offered = assets.from_bills(f.db, a("Chi"))
    assert [c.description for c in offered] == ["Dell Latitude laptops"]
    laptops = offered[0]
    assert laptops.category_id == _category(f, "Computer equipment").id
    with pytest.raises(ValidationFailed) as exc:  # the category's cost account must match
        assets.register(
            f.db,
            a("Chi"),
            None,
            AssetInput(
                name="Laptops",
                category_id=str(_category(f, "Motor vehicles").id),
                acquired_on=None,
                cost="1",
                life_months="48",
                bill_line_id=str(laptops.line_id),
            ),
        )
    assert "category_id" in (exc.value.field_errors or {})
    asset_id, warning = assets.register(
        f.db,
        a("Chi"),
        None,
        AssetInput(
            name="Dell Latitude laptops",
            category_id=str(laptops.category_id),
            acquired_on=None,
            cost="1",  # ignored: the bill line's net
            life_months="36",
            bill_line_id=str(laptops.line_id),
        ),
    )
    row = f.db.get(FixedAsset, asset_id)
    assert row is not None and warning is None
    assert (row.number, row.cost, row.acquired_on) == (
        "FA-00001",
        D("1850000.00"),
        date(2026, 2, 10),
    )
    assert row.supplier_id is not None
    assert assets.from_bills(f.db, a("Chi")) == []
    checks = {c.account: c for c in assets.ledger_check(f.db, a("Chi"))}
    check = checks["1550 Computer equipment"]  # the mouse is only in the ledger
    assert (check.register, check.ledger, check.matches) == (
        D("1850000.00"),
        D("1865000.00"),
        False,
    )

    # An asset from before go-live, with its depreciation so far.
    old = _asset(
        f,
        "Computer equipment",
        "360,000",
        date(2025, 6, 1),
        opening_accumulated="70,000",
        opening_to=date(2025, 12, 31),
    )
    row = f.db.get(FixedAsset, old)
    assert row is not None and row.months_left == 29 and row.depreciated_to == date(2025, 12, 1)
    assert not {c.account: c for c in assets.ledger_check(f.db, a("Chi"))}[
        "1550 Computer equipment"
    ].matches  # its cost isn't in this ledger

    with pytest.raises(ValidationFailed) as exc:
        assets.register(
            f.db,
            a("Chi"),
            None,
            AssetInput(
                name="",
                category_id=str(_category(f, "Computer equipment").id),
                acquired_on=date(2099, 1, 1),
                cost="100",
                residual="100",
                life_months="36",
            ),
        )
    assert {"name", "acquired_on", "residual"} <= set(exc.value.field_errors or {})
    _, warning = assets.register(
        f.db,
        a("Chi"),
        None,
        AssetInput(
            name="Printer",
            category_id=str(_category(f, "Computer equipment").id),
            acquired_on=date(2026, 2, 1),
            cost="80,000",
            life_months="36",
        ),
    )
    assert warning is not None and "threshold" in warning

    # Once depreciation is posted, only the description can change.
    _run(f, date(2026, 3, 1))  # January: only the opening-balance asset is due
    _run(f, date(2026, 3, 1))  # February
    assets.register(
        f.db,
        a("Chi"),
        asset_id,
        AssetInput(
            name="Dell laptops (finance team)",
            category_id=str(_category(f, "Motor vehicles").id),
            acquired_on=date(2026, 1, 1),
            cost="9",
            location="Lagos office",
        ),
    )
    f.db.refresh(row := f.db.get(FixedAsset, asset_id))  # type: ignore[arg-type]
    assert row is not None and row.name == "Dell laptops (finance team)"
    assert row.cost == D("1850000.00") and row.location == "Lagos office"
    with pytest.raises(Conflict):
        assets.delete_asset(f.db, a("Chi"), asset_id)
