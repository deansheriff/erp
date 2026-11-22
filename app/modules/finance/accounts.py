"""The chart of accounts (ADR 0023): hierarchical accounts typed asset, liability, equity,
income or expense. Headers group accounts and take no postings; accounts with postings
can't change type, and are deactivated rather than deleted."""

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance.models import (
    ACCOUNT_TYPES,
    CASH_FLOW_CLASSES,
    Account,
    Journal,
    JournalLine,
)

READ = "finance.account.read"
MANAGE = "finance.account.manage"
CODE = re.compile(r"[A-Za-z0-9.\-]{1,20}")
BALANCE_SHEET = ("asset", "liability", "equity")
DEBIT_NATURE = ("asset", "expense")
TYPE_LABELS = {
    "asset": "Asset",
    "liability": "Liability",
    "equity": "Equity",
    "income": "Income",
    "expense": "Expense",
}
CASH_FLOW_LABELS = {
    "cash": "Cash",
    "operating": "Operating",
    "investing": "Investing",
    "financing": "Financing",
}
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class AccountInput:
    code: str
    name: str
    type: str
    parent_id: uuid.UUID | None
    is_header: bool
    cash_flow: str
    description: str
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class TreeRow:
    account: Account
    depth: int
    balance: Decimal  # in the account's natural direction (debit for assets and expenses)
    has_postings: bool


def signed(account_type: str, debit: Decimal, credit: Decimal) -> Decimal:
    """A balance in the account's natural direction."""
    return debit - credit if account_type in DEBIT_NATURE else credit - debit


def all_accounts(db: Session) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.code)))


def postable(db: Session) -> list[Account]:
    """Active accounts that take postings, for pickers."""
    return list(
        db.scalars(
            select(Account)
            .where(Account.is_active, Account.is_header.is_(False))
            .order_by(Account.code)
        )
    )


def by_codes(db: Session, codes: set[str]) -> dict[str, Account]:
    if not codes:
        return {}
    return {a.code: a for a in db.scalars(select(Account).where(Account.code.in_(codes)))}


def totals(db: Session) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Posted debits and credits by account, all time."""
    rows = db.execute(
        select(JournalLine.account_id, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(Journal.status == "posted")
        .group_by(JournalLine.account_id)
    ).all()
    return {a: (d or ZERO, c or ZERO) for a, d, c in rows if a}


def _used(db: Session, account_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(JournalLine)
            .where(JournalLine.account_id == account_id)
        )
    )


def tree(db: Session, actor: Actor) -> list[TreeRow]:
    """Accounts in code order under their parents, each header with the sum of what it
    groups."""
    if not engine.check(db, actor.user_id, READ):
        raise PermissionDenied()
    accounts = all_accounts(db)
    sums = totals(db)
    children: dict[uuid.UUID | None, list[Account]] = {}
    for a in accounts:
        children.setdefault(a.parent_id, []).append(a)
    known = {a.id for a in accounts}
    rows: list[TreeRow] = []

    def walk(parent: uuid.UUID | None, depth: int) -> Decimal:
        subtotal = ZERO
        for a in children.get(parent, []):
            index = len(rows)
            rows.append(TreeRow(a, depth, ZERO, False))
            debit, credit = sums.get(a.id, (ZERO, ZERO))
            own = signed(a.type, debit, credit)
            below = walk(a.id, depth + 1)
            rows[index] = TreeRow(a, depth, own + below, a.id in sums)
            subtotal += own + below
        return subtotal

    walk(None, 0)
    # Accounts whose parent is missing (shouldn't happen) still show, at the top level.
    for a in accounts:
        if a.parent_id is not None and a.parent_id not in known:
            debit, credit = sums.get(a.id, (ZERO, ZERO))
            rows.append(TreeRow(a, 0, signed(a.type, debit, credit), a.id in sums))
    return rows


def _check(db: Session, account_id: uuid.UUID | None, data: AccountInput) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not CODE.fullmatch(data.code.strip()):
        errors["code"] = "Enter a code: letters, digits, dots or hyphens, up to 20."
    elif db.scalars(
        select(Account).where(Account.code == data.code.strip(), Account.id != account_id)
    ).first():
        errors["code"] = "Another account has this code."
    if not data.name.strip():
        errors["name"] = "Enter a name."
    if data.type not in ACCOUNT_TYPES:
        errors["type"] = "Choose a type."
    if data.cash_flow and data.cash_flow not in CASH_FLOW_CLASSES:
        errors["cash_flow"] = "Choose a cash-flow class."
    if data.cash_flow and data.type not in BALANCE_SHEET:
        errors["cash_flow"] = "Only balance-sheet accounts have a cash-flow class."
    if data.parent_id:
        parent = db.get(Account, data.parent_id)
        if parent is None or not parent.is_header:
            errors["parent_id"] = "Choose a header account as the parent."
        elif parent.type != data.type:
            errors["parent_id"] = "The parent must be the same type."
        else:
            seen: set[uuid.UUID] = set()
            cursor: Account | None = parent
            while cursor is not None and cursor.id not in seen:
                if cursor.id == account_id:
                    errors["parent_id"] = "An account can't sit under itself."
                    break
                seen.add(cursor.id)
                cursor = db.get(Account, cursor.parent_id) if cursor.parent_id else None
    return errors


def save_account(
    db: Session, actor: Actor, account_id: uuid.UUID | None, data: AccountInput
) -> uuid.UUID:
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()
    errors = _check(db, account_id, data)
    row = db.get(Account, account_id) if account_id else None
    if account_id and row is None:
        raise NotFound()
    if row is not None and _used(db, row.id):
        if data.type != row.type:
            errors["type"] = "This account has postings, so its type can't change."
        if data.is_header and not row.is_header:
            errors["is_header"] = "This account has postings, so it can't become a header."
    if (
        row is not None
        and not data.is_header
        and row.is_header
        and db.scalars(select(Account).where(Account.parent_id == row.id)).first()
    ):
        errors["is_header"] = "Accounts sit under this header; move them first."
    if errors:
        raise ValidationFailed(field_errors=errors)
    values = {
        "code": data.code.strip(),
        "name": data.name.strip(),
        "type": data.type,
        "parent_id": data.parent_id,
        "is_header": data.is_header,
        "cash_flow": data.cash_flow or None,
        "description": data.description.strip() or None,
        "is_active": data.is_active,
    }
    before = {k: getattr(row, k) for k in values} if row else {}
    if row is None:
        row = Account(**values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    record_by(
        db,
        actor,
        "finance.account.save",
        entity_type="finance_account",
        entity_id=str(row.id),
        changes=diff_changes(before, values),
    )
    return row.id
