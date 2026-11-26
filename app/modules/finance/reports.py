"""Financial reports from posted journals (ADR 0023): trial balance, profit and loss,
balance sheet, cash flow (indirect) and the general ledger for an account. The year-end
closing journal is left out of profit and loss and cash flow."""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import engine
from app.core.errors import NotFound, PermissionDenied
from app.identity.schemas import Actor
from app.modules.finance.accounts import BALANCE_SHEET, signed
from app.modules.finance.models import Account, Journal, JournalLine

READ = "finance.report.read"
ZERO = Decimal(0)
Sums = dict[uuid.UUID, tuple[Decimal, Decimal]]


@dataclass(frozen=True, slots=True)
class Row:
    code: str
    name: str
    amount: Decimal
    account_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    rows: list[Row]

    @property
    def total(self) -> Decimal:
        return sum((r.amount for r in self.rows), ZERO)


@dataclass(frozen=True, slots=True)
class TrialRow:
    code: str
    name: str
    debit: Decimal
    credit: Decimal
    account_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TrialBalance:
    as_at: date
    rows: list[TrialRow]

    @property
    def debit(self) -> Decimal:
        return sum((r.debit for r in self.rows), ZERO)

    @property
    def credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), ZERO)


@dataclass(frozen=True, slots=True)
class ProfitAndLoss:
    start: date
    end: date
    income: list[Section]
    expenses: list[Section]

    @property
    def total_income(self) -> Decimal:
        return sum((s.total for s in self.income), ZERO)

    @property
    def total_expenses(self) -> Decimal:
        return sum((s.total for s in self.expenses), ZERO)

    @property
    def net_profit(self) -> Decimal:
        return self.total_income - self.total_expenses


@dataclass(frozen=True, slots=True)
class BalanceSheet:
    as_at: date
    assets: list[Section]
    liabilities: list[Section]
    equity: list[Section]
    unclosed_profit: Decimal

    @property
    def total_assets(self) -> Decimal:
        return sum((s.total for s in self.assets), ZERO)

    @property
    def total_liabilities(self) -> Decimal:
        return sum((s.total for s in self.liabilities), ZERO)

    @property
    def total_equity(self) -> Decimal:
        return sum((s.total for s in self.equity), ZERO) + self.unclosed_profit

    @property
    def balances(self) -> bool:
        return self.total_assets == self.total_liabilities + self.total_equity


@dataclass(frozen=True, slots=True)
class CashFlow:
    start: date
    end: date
    net_profit: Decimal
    operating: list[Row]
    investing: list[Row]
    financing: list[Row]
    opening_cash: Decimal
    closing_cash: Decimal

    @property
    def operating_total(self) -> Decimal:
        return self.net_profit + sum((r.amount for r in self.operating), ZERO)

    @property
    def investing_total(self) -> Decimal:
        return sum((r.amount for r in self.investing), ZERO)

    @property
    def financing_total(self) -> Decimal:
        return sum((r.amount for r in self.financing), ZERO)

    @property
    def net_change(self) -> Decimal:
        return self.operating_total + self.investing_total + self.financing_total


@dataclass(frozen=True, slots=True)
class LedgerLine:
    journal_id: uuid.UUID
    journal_date: date
    number: str
    memo: str
    description: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class Ledger:
    account: Account
    start: date
    end: date
    opening: Decimal
    lines: list[LedgerLine] = field(default_factory=list)

    @property
    def closing(self) -> Decimal:
        return self.lines[-1].balance if self.lines else self.opening


def _require(db: Session, actor: Actor) -> None:
    if not engine.check(db, actor.user_id, READ):
        raise PermissionDenied()


def _sums(
    db: Session,
    *,
    start: date | None = None,
    end: date,
    with_year_end: bool = True,
    cost_centre_id: uuid.UUID | None = None,
) -> Sums:
    query = (
        select(JournalLine.account_id, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(Journal.status == "posted", Journal.journal_date <= end)
        .group_by(JournalLine.account_id)
    )
    if start:
        query = query.where(Journal.journal_date >= start)
    if not with_year_end:
        query = query.where(Journal.source != "year_end")
    if cost_centre_id:
        query = query.where(JournalLine.cost_centre_id == cost_centre_id)
    return {a: (d or ZERO, c or ZERO) for a, d, c in db.execute(query).all() if a}


def _accounts(db: Session) -> dict[uuid.UUID, Account]:
    return {a.id: a for a in db.scalars(select(Account).order_by(Account.code))}


def _sections(
    accounts: dict[uuid.UUID, Account], sums: Sums, types: tuple[str, ...]
) -> list[Section]:
    """Non-zero accounts of these types grouped under their parent header, in code order."""
    groups: dict[uuid.UUID | None, list[Row]] = {}
    for account in sorted(accounts.values(), key=lambda a: a.code):
        if account.type not in types or account.id not in sums:
            continue
        amount = signed(account.type, *sums[account.id])
        if amount:
            groups.setdefault(account.parent_id, []).append(
                Row(account.code, account.name, amount, account.id)
            )
    sections = []
    for parent_id, rows in groups.items():
        parent = accounts.get(parent_id) if parent_id else None
        sections.append(Section(parent.name if parent else "Other", rows))
    return sorted(sections, key=lambda s: s.rows[0].code)


def trial_balance(db: Session, actor: Actor, as_at: date) -> TrialBalance:
    _require(db, actor)
    accounts = _accounts(db)
    rows = []
    for account_id, (debit, credit) in _sums(db, end=as_at).items():
        account = accounts[account_id]
        net = debit - credit
        if net:
            rows.append(
                TrialRow(
                    account.code,
                    account.name,
                    net if net > 0 else ZERO,
                    -net if net < 0 else ZERO,
                    account.id,
                )
            )
    return TrialBalance(as_at, sorted(rows, key=lambda r: r.code))


def profit_and_loss(
    db: Session, actor: Actor, start: date, end: date, cost_centre_id: uuid.UUID | None = None
) -> ProfitAndLoss:
    _require(db, actor)
    accounts = _accounts(db)
    sums = _sums(db, start=start, end=end, with_year_end=False, cost_centre_id=cost_centre_id)
    return ProfitAndLoss(
        start,
        end,
        _sections(accounts, sums, ("income",)),
        _sections(accounts, sums, ("expense",)),
    )


def balance_sheet(db: Session, actor: Actor, as_at: date) -> BalanceSheet:
    _require(db, actor)
    accounts = _accounts(db)
    sums = _sums(db, end=as_at)
    unclosed = ZERO
    for account_id, (debit, credit) in sums.items():
        if accounts[account_id].type in ("income", "expense"):
            unclosed += credit - debit
    return BalanceSheet(
        as_at,
        _sections(accounts, sums, ("asset",)),
        _sections(accounts, sums, ("liability",)),
        _sections(accounts, sums, ("equity",)),
        unclosed,
    )


def cash_flow(db: Session, actor: Actor, start: date, end: date) -> CashFlow:
    """Indirect method: profit, then the cash effect of each balance-sheet account's
    movement by its cash-flow class (unclassified ones count as operating)."""
    _require(db, actor)
    accounts = _accounts(db)
    moved = _sums(db, start=start, end=end, with_year_end=False)
    profit = ZERO
    groups: dict[str, list[Row]] = {"operating": [], "investing": [], "financing": []}
    cash_change = ZERO
    for account_id, (debit, credit) in sorted(moved.items(), key=lambda kv: accounts[kv[0]].code):
        account = accounts[account_id]
        if account.type in ("income", "expense"):
            profit += credit - debit
        elif account.cash_flow == "cash":
            cash_change += debit - credit
        elif credit - debit:
            groups[account.cash_flow or "operating"].append(
                Row(account.code, account.name, credit - debit, account.id)
            )
    before = date.fromordinal(start.toordinal() - 1)
    opening = sum(
        (
            debit - credit
            for account_id, (debit, credit) in _sums(db, end=before).items()
            if accounts[account_id].cash_flow == "cash"
        ),
        ZERO,
    )
    return CashFlow(
        start,
        end,
        profit,
        groups["operating"],
        groups["investing"],
        groups["financing"],
        opening,
        opening + cash_change,
    )


def general_ledger(
    db: Session, actor: Actor, account_id: uuid.UUID, start: date, end: date
) -> Ledger:
    _require(db, actor)
    account = db.get(Account, account_id)
    if account is None:
        raise NotFound()
    before = date.fromordinal(start.toordinal() - 1)
    debit, credit = _sums(db, end=before).get(account.id, (ZERO, ZERO))
    opening = signed(account.type, debit, credit)
    running = opening
    lines = []
    for line, journal in db.execute(
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            JournalLine.account_id == account.id,
            Journal.status == "posted",
            Journal.journal_date >= start,
            Journal.journal_date <= end,
        )
        .order_by(Journal.journal_date, Journal.number, JournalLine.position)
    ).all():
        running += signed(account.type, line.debit, line.credit)
        lines.append(
            LedgerLine(
                journal.id,
                journal.journal_date,
                journal.number or "",
                journal.memo,
                line.description,
                line.debit,
                line.credit,
                running,
            )
        )
    return Ledger(account, start, end, opening, lines)


def is_balance_sheet(account: Account) -> bool:
    return account.type in BALANCE_SHEET
