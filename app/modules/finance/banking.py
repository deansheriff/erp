"""Bank accounts and matching rules (ADR 0026). Each bank account has its own ledger
account under 10 Cash and bank, created with it, and a layout for its CSV and Excel
statements. Rules turn statement lines into entries by what their description contains."""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts
from app.modules.finance.models import (
    RULE_DIRECTIONS,
    Account,
    BankAccount,
    BankReconciliation,
    BankRule,
    Journal,
    JournalLine,
    StatementLine,
)
from app.org import service as org

READ = "finance.bank.read"
MANAGE = "finance.bank.manage"
RECONCILE = "finance.bank.reconcile"
APPROVE = "finance.bank.approve"
HEADER_CODE = "10"
NUBAN = re.compile(r"^\d{10}$")
BANK_CODE = re.compile(r"^\d{3,6}$")
ZERO = Decimal(0)
DIRECTION_LABELS = {"in": "Money in", "out": "Money out", "any": "Either"}


def can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor, permission):
        raise PermissionDenied()


def uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


# ------------------------------------------------------------------ accounts


@dataclass(frozen=True, slots=True)
class BankAccountInput:
    name: str  # the ledger account's name, e.g. "GTBank · Operations"
    bank_name: str
    bank_code: str
    account_number: str
    account_name: str
    csv_date: str
    csv_description: str
    csv_reference: str
    csv_amount: str
    csv_money_in: str
    csv_money_out: str
    csv_date_format: str
    csv_skip_rows: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class BankAccountRow:
    id: uuid.UUID
    account_id: uuid.UUID
    code: str
    name: str
    bank: str | None
    is_active: bool
    balance: Decimal
    unmatched: int
    last_reconciled: tuple[str, Decimal] | None  # (date, statement balance)


def _next_code(db: Session) -> str:
    """The next free code in 1011 to 1099 (1020 is cash on hand)."""
    used = {a.code for a in accounts.all_accounts(db)}
    for n in range(1011, 1100):
        if str(n) not in used:
            return str(n)
    raise Conflict("Every code from 1011 to 1099 is taken. Add the account by hand.")


def _layout_errors(data: BankAccountInput) -> dict[str, str]:
    errors = {}
    if not data.csv_date.strip():
        errors["csv_date"] = "Say which column holds the date."
    if not data.csv_description.strip():
        errors["csv_description"] = "Say which column holds the description."
    if not data.csv_amount.strip() and not (
        data.csv_money_in.strip() and data.csv_money_out.strip()
    ):
        errors["csv_amount"] = "Give a signed amount column, or both money-in and money-out."
    if "%" not in data.csv_date_format:
        errors["csv_date_format"] = "Give the date format, like %d/%m/%Y."
    try:
        if not 0 <= int(data.csv_skip_rows.strip() or "0") <= 50:
            raise ValueError
    except ValueError:
        errors["csv_skip_rows"] = "Enter a number of rows from 0 to 50."
    return errors


def _bank_errors(data: BankAccountInput) -> dict[str, str]:
    errors = {}
    if data.bank_code.strip() and not BANK_CODE.match(data.bank_code.strip()):
        errors["bank_code"] = "Enter the bank's CBN code, 3 to 6 digits."
    if data.account_number.strip() and not NUBAN.match(data.account_number.strip()):
        errors["account_number"] = "Enter the 10-digit account number."
    return errors


def save_account(
    db: Session, actor: Actor, bank_id: uuid.UUID | None, data: BankAccountInput
) -> uuid.UUID:
    """Add a bank account (with its new ledger account) or change one."""
    require(db, actor, MANAGE)
    errors = {**_layout_errors(data), **_bank_errors(data)}
    if not data.name.strip():
        errors["name"] = "Name the account, like GTBank · Operations."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(BankAccount, bank_id) if bank_id else None
    if bank_id and row is None:
        raise NotFound()
    if row is None:
        header = accounts.by_codes(db, {HEADER_CODE}).get(HEADER_CODE)
        ledger = Account(
            code=_next_code(db),
            name=data.name.strip()[:120],
            type="asset",
            parent_id=header.id if header else None,
            cash_flow="cash",
        )
        db.add(ledger)
        db.flush()
        row = BankAccount(account_id=ledger.id)
        db.add(row)
    else:
        found = db.get(Account, row.account_id)
        assert found is not None  # noqa: S101  (foreign key)
        ledger = found
        ledger.name = data.name.strip()[:120]
    row.bank_name = data.bank_name.strip()[:120] or None
    row.bank_code = data.bank_code.strip() or None
    row.account_number = data.account_number.strip() or None
    row.account_name = data.account_name.strip()[:200] or None
    row.csv_date, row.csv_description = data.csv_date.strip(), data.csv_description.strip()
    row.csv_reference = data.csv_reference.strip() or None
    row.csv_amount = data.csv_amount.strip() or None
    row.csv_money_in = data.csv_money_in.strip() or None
    row.csv_money_out = data.csv_money_out.strip() or None
    row.csv_date_format = data.csv_date_format.strip()
    row.csv_skip_rows = int(data.csv_skip_rows.strip() or "0")
    row.is_active = data.is_active
    ledger.is_active = data.is_active
    db.flush()
    record_by(
        db,
        actor,
        "finance.bank_account.save",
        entity_type="bank_account",
        entity_id=str(row.id),
        changes={"ledger": ledger.code, "bank": row.bank_name, "active": row.is_active},
    )
    return row.id


def get_account(db: Session, actor: Actor, bank_id: uuid.UUID) -> BankAccount:
    require(db, actor, READ)
    row = db.get(BankAccount, bank_id)
    if row is None:
        raise NotFound()
    return row


def ledger_balance(db: Session, account_id: uuid.UUID, *, as_at: date | None = None) -> Decimal:
    query = (
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(JournalLine.account_id == account_id, Journal.status == "posted")
    )
    if as_at is not None:
        query = query.where(Journal.journal_date <= as_at)
    return Decimal(db.scalar(query) or 0)


def list_accounts(db: Session, actor: Actor) -> list[BankAccountRow]:
    require(db, actor, READ)
    out = []
    rows = db.execute(
        select(BankAccount, Account)
        .join(Account, Account.id == BankAccount.account_id)
        .order_by(Account.code)
    ).all()
    for bank, ledger in rows:
        unmatched = db.scalar(
            select(func.count()).where(
                StatementLine.bank_account_id == bank.id, StatementLine.status == "unmatched"
            )
        )
        last = db.scalars(
            select(BankReconciliation)
            .where(
                BankReconciliation.bank_account_id == bank.id,
                BankReconciliation.status == "approved",
            )
            .order_by(BankReconciliation.statement_date.desc())
            .limit(1)
        ).first()
        out.append(
            BankAccountRow(
                bank.id,
                ledger.id,
                ledger.code,
                ledger.name,
                f"{bank.bank_name} ••••{bank.account_number[-4:]}"
                if bank.bank_name and bank.account_number
                else None,
                bank.is_active,
                ledger_balance(db, ledger.id),
                unmatched or 0,
                (last.statement_date.isoformat(), last.statement_balance) if last else None,
            )
        )
    return out


def choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    """Active bank accounts, for pickers."""
    return [
        (bank.id, f"{ledger.code} {ledger.name}")
        for bank, ledger in db.execute(
            select(BankAccount, Account)
            .join(Account, Account.id == BankAccount.account_id)
            .where(BankAccount.is_active)
            .order_by(Account.code)
        ).all()
    ]


def ledger_of(db: Session, bank_id: uuid.UUID) -> Account:
    bank = db.get(BankAccount, bank_id)
    ledger = db.get(Account, bank.account_id) if bank else None
    if ledger is None:
        raise NotFound()
    return ledger


# ------------------------------------------------------------------ rules


@dataclass(frozen=True, slots=True)
class RuleInput:
    bank_account_id: str
    contains: str
    direction: str
    account_id: str
    cost_centre_id: str
    description: str
    is_active: bool


def rules(db: Session) -> list[BankRule]:
    return list(db.scalars(select(BankRule).order_by(BankRule.position)))


def save_rule(db: Session, actor: Actor, rule_id: uuid.UUID | None, data: RuleInput) -> uuid.UUID:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    contains = data.contains.strip()
    if len(contains) < 2:
        errors["contains"] = "Enter the text the description contains, at least 2 characters."
    if data.direction not in RULE_DIRECTIONS:
        errors["direction"] = "Choose money in, money out or either."
    account = {a.id: a for a in accounts.postable(db)}.get(uuid_or_none(data.account_id))  # type: ignore[arg-type]
    if account is None:
        errors["account_id"] = "Choose the account it goes to."
    bank_id = uuid_or_none(data.bank_account_id)
    if data.bank_account_id and (bank_id is None or db.get(BankAccount, bank_id) is None):
        errors["bank_account_id"] = "Choose a bank account, or all of them."
    centre = uuid_or_none(data.cost_centre_id)
    if centre and centre not in {cid for cid, _ in org.cost_centre_choices(db)}:
        errors["cost_centre_id"] = "Choose an active cost centre."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert account is not None  # noqa: S101  (checked above)
    row = db.get(BankRule, rule_id) if rule_id else None
    if row is None:
        position = (db.scalar(select(func.max(BankRule.position))) or 0) + 1
        row = BankRule(contains=contains, account_id=account.id, position=position)
        db.add(row)
    row.bank_account_id, row.contains, row.direction = bank_id, contains[:120], data.direction
    row.account_id, row.cost_centre_id = account.id, centre
    row.description = data.description.strip()[:200] or None
    row.is_active = data.is_active
    db.flush()
    record_by(
        db,
        actor,
        "finance.bank_rule.save",
        entity_type="bank_rule",
        entity_id=str(row.id),
        changes={"contains": contains, "account": account.code, "active": data.is_active},
    )
    return row.id


def rule_for(
    rule_list: list[BankRule], bank_id: uuid.UUID, description: str, amount: Decimal
) -> BankRule | None:
    """The first active rule, in order, that fits the line."""
    text = description.lower()
    for rule in rule_list:
        if not rule.is_active or (rule.bank_account_id and rule.bank_account_id != bank_id):
            continue
        if rule.direction == "in" and amount < 0:
            continue
        if rule.direction == "out" and amount > 0:
            continue
        if rule.contains.lower() in text:
            return rule
    return None
