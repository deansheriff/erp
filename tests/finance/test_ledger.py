"""Phase 10a: the chart of accounts, journals with maker-checker, system journals posted or
held, periods and the year end, recurring journals, and the reports (ADR 0023)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.modules.finance import accounts, journals, periods, recurring, reports
from app.modules.finance.accounts import AccountInput
from app.modules.finance.models import FiscalPeriod, Journal
from tests.finance.conftest import Finance

pytestmark = pytest.mark.db


def _period(f: Finance, month: int) -> uuid.UUID:
    return f.db.scalars(
        select(FiscalPeriod.id).where(
            FiscalPeriod.year_id == f.year_id, FiscalPeriod.number == month
        )
    ).one()


# ------------------------------------------------------------------ chart


def test_the_chart_is_seeded_and_editable(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    rows = accounts.tree(db, a("Chi"))
    codes = [r.account.code for r in rows]
    assert codes[:3] == ["1", "10", "1010"] and "6000" in codes and "2170" in codes
    with pytest.raises(PermissionDenied):
        accounts.save_account(
            db, a("Chi"), None, AccountInput("6600", "Security", "expense", None, False, "", "")
        )
    header = finance.account("62")
    with pytest.raises(ValidationFailed) as exc:
        accounts.save_account(
            db, a("Ada"), None, AccountInput("6600", "Security", "income", header.id, False, "", "")
        )
    assert "parent_id" in (exc.value.field_errors or {})
    new = accounts.save_account(
        db, a("Ada"), None, AccountInput("6600", "Security", "expense", header.id, False, "", "")
    )
    finance.posted(date(2026, 3, 5), [("6600", "1,000", ""), ("1010", "", "1,000")])
    with pytest.raises(ValidationFailed) as exc:
        accounts.save_account(
            db, a("Ada"), new, AccountInput("6600", "Security", "asset", None, False, "", "")
        )
    assert "type" in (exc.value.field_errors or {})


# ------------------------------------------------------------------ journals


def test_someone_else_posts_a_draft_and_numbers_are_gap_free(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    draft = journals.save_draft(
        db,
        a("Chi"),
        None,
        journal_date=date(2026, 4, 1),
        memo="Office rent",
        lines=[finance.line("6400", "500,000"), finance.line("1010", "", "450,000")],
    )
    with pytest.raises(PermissionDenied):
        journals.post(db, a("Chi"), draft)  # officers draft; managers post
    with pytest.raises(Conflict):
        journals.post(db, a("Ada"), draft)  # out of balance
    journals.save_draft(
        db,
        a("Chi"),
        draft,
        journal_date=date(2026, 4, 1),
        memo="Office rent",
        lines=[finance.line("6400", "500,000"), finance.line("1010", "", "500,000")],
    )
    other = journals.save_draft(
        db,
        a("Ada"),
        None,
        journal_date=date(2026, 4, 2),
        memo="Fuel",
        lines=[finance.line("6130", "20,000"), finance.line("1020", "", "20,000")],
    )
    with pytest.raises(PermissionDenied):
        journals.post(db, a("Ada"), other)  # she drafted it
    assert journals.post(db, a("Bola"), other) == "JV-00001"
    assert journals.post(db, a("Ada"), draft) == "JV-00002"
    with pytest.raises(Conflict):
        journals.save_draft(db, a("Ada"), draft, journal_date=date(2026, 4, 1), memo="x", lines=[])
    with pytest.raises(Conflict):
        journals.delete_draft(db, a("Ada"), draft)


def test_lines_need_an_account_and_one_side(finance: Finance) -> None:
    with pytest.raises(ValidationFailed) as exc:
        journals.save_draft(
            finance.db,
            finance.hr.actor("Ada"),
            None,
            journal_date=date(2026, 4, 1),
            memo="Bad",
            lines=[
                finance.line("6400", "100", "100"),
                finance.line("6", "100"),  # a header
                journals.LineInput("", "", "", "abc", ""),
            ],
        )
    assert set(exc.value.field_errors or {}) == {"line_0", "line_1", "line_2"}


def test_reversal_swaps_the_lines(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    original = finance.posted(date(2026, 5, 1), [("6400", "300,000", ""), ("1010", "", "300,000")])
    reversal = journals.reverse(db, a("Ada"), original, on=date(2026, 5, 31))
    view = journals.get_journal(db, a("Chi"), reversal)
    assert view.source == "reversal" and view.reverses and view.reverses[0] == original
    assert [(line.account_code, line.debit, line.credit) for line in view.lines] == [
        ("6400", Decimal(0), Decimal("300000.00")),
        ("1010", Decimal("300000.00"), Decimal(0)),
    ]
    with pytest.raises(Conflict):
        journals.reverse(db, a("Ada"), original, on=date(2026, 5, 31))
    assert reports.trial_balance(db, a("Ada"), date(2026, 5, 31)).rows == []


def test_system_journals_post_at_once_or_are_held(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    lines = [
        ("6100", None, Decimal("18500.00"), Decimal(0)),
        ("1010", None, Decimal(0), Decimal("18500.00")),
    ]
    posted_id, posted = journals.post_system_journal(
        db,
        source="expenses",
        source_id=uuid.uuid4(),
        journal_date=date(2026, 6, 3),
        memo="EXP-1",
        lines=lines,
    )
    assert posted
    source = uuid.uuid4()
    held_id, posted = journals.post_system_journal(
        db,
        source="payroll",
        source_id=source,
        journal_date=date(2025, 12, 31),
        memo="Old run",
        lines=lines,
    )
    assert not posted
    assert journals.statuses(db, "payroll", [source]) == {source: ("held", None)}
    held = db.get(Journal, held_id)
    assert held is not None and held.hold_reason == "The financial year 2025 isn't open."
    # handing the same document over again changes nothing
    assert journals.post_system_journal(
        db,
        source="payroll",
        source_id=source,
        journal_date=date(2025, 12, 31),
        memo="x",
        lines=lines,
    ) == (held_id, False)
    with pytest.raises(ValidationFailed):
        journals.redate_held(db, a("Ada"), held_id, new_date=date(2026, 1, 31), reason="")
    number = journals.redate_held(
        db, a("Ada"), held_id, new_date=date(2026, 1, 31), reason="2025 was never opened"
    )
    assert number == "JV-00002"
    view = journals.get_journal(db, a("Chi"), held_id)
    assert view.original_date == date(2025, 12, 31) and view.status == "posted"
    unknown, posted = journals.post_system_journal(
        db,
        source="expenses",
        source_id=uuid.uuid4(),
        journal_date=date(2026, 6, 3),
        memo="Odd account",
        lines=[("9999", None, Decimal(5), Decimal(0)), ("1010", None, Decimal(0), Decimal(5))],
    )
    assert not posted and "9999" in (db.get(Journal, unknown).hold_reason or "")  # type: ignore[union-attr]
    assert posted_id


# ------------------------------------------------------------------ periods and the year end


def test_closing_and_reopening_months(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    march = _period(finance, 3)
    journals.save_draft(
        db,
        a("Chi"),
        None,
        journal_date=date(2026, 3, 10),
        memo="Pending",
        lines=[finance.line("6400", "1"), finance.line("1010", "", "1")],
    )
    with pytest.raises(Conflict) as exc:
        periods.close_period(db, a("Ada"), march)
    assert "1 draft journal is dated in March" in exc.value.message
    draft = db.scalars(select(Journal.id).where(Journal.status == "draft")).one()
    journals.post(db, a("Ada"), draft)
    with pytest.raises(PermissionDenied):
        periods.close_period(db, a("Chi"), march)
    periods.close_period(db, a("Ada"), march)
    draft = journals.save_draft(
        db,
        a("Chi"),
        None,
        journal_date=date(2026, 3, 11),
        memo="Late",
        lines=[finance.line("6400", "2"), finance.line("1010", "", "2")],
    )
    with pytest.raises(Conflict) as exc:
        journals.post(db, a("Ada"), draft)
    assert exc.value.message == "March 2026 is closed."
    with pytest.raises(ValidationFailed):
        periods.reopen_period(db, a("Ada"), march, reason="")
    periods.reopen_period(db, a("Ada"), march, reason="Late supplier invoice")
    journals.post(db, a("Ada"), draft)


def test_closing_the_year_moves_profit_to_retained_earnings(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    finance.posted(date(2026, 2, 1), [("1010", "1,000,000", ""), ("4100", "", "1,000,000")])
    finance.posted(date(2026, 2, 2), [("6400", "400,000", ""), ("1010", "", "400,000")])
    with pytest.raises(Conflict):
        journals.close_year(db, a("Ada"), finance.year_id)
    for month in range(1, 13):
        periods.close_period(db, a("Ada"), _period(finance, month))
    closing = journals.close_year(db, a("Ada"), finance.year_id)
    assert closing is not None
    bs = reports.balance_sheet(db, a("Ada"), date(2026, 12, 31))
    assert bs.unclosed_profit == 0 and bs.balances
    assert [r.amount for s in bs.equity for r in s.rows if r.code == "3200"] == [
        Decimal("600000.00")
    ]
    pl = reports.profit_and_loss(db, a("Ada"), date(2026, 1, 1), date(2026, 12, 31))
    assert pl.net_profit == Decimal("600000.00")  # the closing journal is left out
    with pytest.raises(Conflict):
        periods.reopen_period(db, a("Ada"), _period(finance, 12), reason="Try again")
    with pytest.raises(Conflict):
        journals.reverse(db, a("Ada"), closing, on=date(2026, 12, 31))


# ------------------------------------------------------------------ recurring


def test_recurring_journals_are_drafted_for_someone_else_to_post(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    with pytest.raises(ValidationFailed):
        recurring.save_template(
            db,
            a("Ada"),
            None,
            name="Rent",
            memo="Monthly rent",
            day_of_month="31",
            first_month=date(2026, 1, 1),
            last_month=None,
            is_active=True,
            lines=[finance.line("6400", "850,000")],
        )
    template = recurring.save_template(
        db,
        a("Ada"),
        None,
        name="Rent",
        memo="Monthly rent accrual",
        day_of_month="1",
        first_month=date(2026, 11, 1),
        last_month=date(2027, 1, 1),
        is_active=True,
        lines=[finance.line("6400", "850,000"), finance.line("2020", "", "850,000")],
    )
    assert recurring.generate_due(db, date(2026, 10, 31)) == 0
    assert recurring.generate_due(db, date(2026, 12, 2)) == 2  # November and December
    assert recurring.generate_due(db, date(2026, 12, 2)) == 0
    assert recurring.generate_due(db, date(2027, 6, 1)) == 1  # January, the last
    drafts = list(db.scalars(select(Journal).where(Journal.recurring_id == template)))
    assert len(drafts) == 3 and {d.status for d in drafts} == {"draft"}
    with pytest.raises(PermissionDenied):
        journals.post(db, a("Ada"), drafts[0].id)  # she wrote the template
    journals.post(db, a("Bola"), drafts[0].id)
    [view] = recurring.list_templates(db, a("Chi"))
    assert view.next_date is None and view.total == Decimal("850000.00")


# ------------------------------------------------------------------ reports


def test_reports_add_up(finance: Finance) -> None:
    db, a = finance.db, finance.hr.actor
    finance.posted(date(2026, 1, 2), [("1010", "5,000,000", ""), ("3100", "", "5,000,000")])
    finance.posted(date(2026, 2, 3), [("1550", "1,200,000", ""), ("1010", "", "1,200,000")])
    finance.posted(date(2026, 3, 4), [("1100", "2,000,000", ""), ("4100", "", "2,000,000")])
    finance.posted(date(2026, 3, 9), [("1010", "1,500,000", ""), ("1100", "", "1,500,000")])
    finance.posted(date(2026, 4, 1), [("6400", "300,000", ""), ("2020", "", "300,000")])
    end = date(2026, 6, 30)

    tb = reports.trial_balance(db, a("Ada"), end)
    assert tb.debit == tb.credit

    pl = reports.profit_and_loss(db, a("Ada"), date(2026, 1, 1), end)
    assert (pl.total_income, pl.total_expenses, pl.net_profit) == (
        Decimal("2000000.00"),
        Decimal("300000.00"),
        Decimal("1700000.00"),
    )

    bs = reports.balance_sheet(db, a("Ada"), end)
    assert bs.balances and bs.total_assets == Decimal("7000000.00")

    cf = reports.cash_flow(db, a("Ada"), date(2026, 1, 1), end)
    assert cf.opening_cash == 0 and cf.closing_cash == Decimal("5300000.00")
    assert cf.net_change == cf.closing_cash - cf.opening_cash
    assert cf.investing_total == Decimal("-1200000.00")
    assert cf.financing_total == Decimal("5000000.00")
    assert cf.operating_total == Decimal(
        "1500000.00"
    )  # 1.7m profit, -0.5m receivables, +0.3m accrual

    ledger = reports.general_ledger(db, a("Ada"), finance.account("1010").id, date(2026, 2, 1), end)
    assert ledger.opening == Decimal("5000000.00")
    assert [line.balance for line in ledger.lines] == [Decimal("3800000.00"), Decimal("5300000.00")]
    with pytest.raises(PermissionDenied):
        reports.trial_balance(db, a("Efe"), end)
