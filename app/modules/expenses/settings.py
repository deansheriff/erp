"""Expense settings (ADR 0020): categories, approval amounts, spending limits and the
ledger accounts journals are queued against."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.core.money import parse_money
from app.identity.schemas import Actor
from app.modules.expenses.models import ExpenseCategory, ExpenseSettings, PersonLimit
from app.modules.hr import service as people

MANAGE = "expenses.settings.manage"


def _require(db: Session, actor: Actor) -> None:
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()


def get_settings(db: Session) -> ExpenseSettings:
    return db.scalars(select(ExpenseSettings)).one()


def _settings_values(row: ExpenseSettings) -> dict[str, object]:
    return {
        "second_level_above": str(row.second_level_above),
        "finance_above": str(row.finance_above),
        "weekly_limit": str(row.weekly_limit) if row.weekly_limit else None,
        "monthly_limit": str(row.monthly_limit) if row.monthly_limit else None,
        "advances_account": row.advances_account,
        "bank_account": row.bank_account,
    }


def _account(value: str, field: str) -> str:
    code = value.strip()
    if not code or len(code) > 20:
        raise ValidationFailed(
            field_errors={field: "Enter the account code (up to 20 characters)."}
        )
    return code


def save_settings(
    db: Session,
    actor: Actor,
    *,
    second_level_above: str,
    finance_above: str,
    weekly_limit: str,
    monthly_limit: str,
    advances_account: str,
    bank_account: str,
) -> None:
    _require(db, actor)
    row = get_settings(db)
    before = _settings_values(row)
    second = parse_money(second_level_above, "second_level_above")
    finance = parse_money(finance_above, "finance_above")
    weekly = parse_money(weekly_limit, "weekly_limit", blank=True)
    monthly = parse_money(monthly_limit, "monthly_limit", blank=True)
    assert second is not None and finance is not None  # noqa: S101
    if finance < second:
        raise ValidationFailed(
            field_errors={"finance_above": "Make this at least the second-level amount."}
        )
    for name, value in (("weekly_limit", weekly), ("monthly_limit", monthly)):
        if value is not None and value <= 0:
            raise ValidationFailed(
                field_errors={name: "Enter an amount above zero, or leave it blank."}
            )
    row.second_level_above, row.finance_above = second, finance
    row.weekly_limit, row.monthly_limit = weekly, monthly
    row.advances_account = _account(advances_account, "advances_account")
    row.bank_account = _account(bank_account, "bank_account")
    record_by(
        db,
        actor,
        "expenses.settings.update",
        entity_type="expense_settings",
        entity_id=str(row.id),
        changes=diff_changes(before, _settings_values(row)),
    )


# ------------------------------------------------------------------ categories


def list_categories(db: Session, *, active_only: bool = False) -> list[ExpenseCategory]:
    stmt = select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
    if active_only:
        stmt = stmt.where(ExpenseCategory.is_active)
    return list(db.scalars(stmt))


def _category_values(row: ExpenseCategory) -> dict[str, object]:
    return {
        "name": row.name,
        "gl_account": row.gl_account,
        "claim_limit": str(row.claim_limit) if row.claim_limit else None,
        "receipt_above": str(row.receipt_above),
        "active": row.is_active,
    }


def save_category(
    db: Session,
    actor: Actor,
    category_id: uuid.UUID | None,
    *,
    name: str,
    gl_account: str,
    claim_limit: str,
    receipt_above: str,
    is_active: bool,
) -> uuid.UUID:
    _require(db, actor)
    name = " ".join(name.split())
    if not name:
        raise ValidationFailed(field_errors={"name": "Give the category a name."})
    clash = db.scalar(
        select(ExpenseCategory.id).where(
            func.lower(ExpenseCategory.name) == name.lower(),
            *([ExpenseCategory.id != category_id] if category_id else []),
        )
    )
    if clash:
        raise ValidationFailed(field_errors={"name": "Another category has this name."})
    limit = parse_money(claim_limit, "claim_limit", blank=True)
    if limit is not None and limit <= 0:
        raise ValidationFailed(
            field_errors={"claim_limit": "Enter an amount above zero, or leave it blank."}
        )
    receipt = parse_money(receipt_above or "0", "receipt_above")
    assert receipt is not None  # noqa: S101
    account = _account(gl_account, "gl_account")
    if category_id is None:
        row = ExpenseCategory(
            name=name,
            gl_account=account,
            claim_limit=limit,
            receipt_above=receipt,
            is_active=True,
            sort_order=(db.scalar(select(func.max(ExpenseCategory.sort_order))) or 0) + 1,
        )
        db.add(row)
        db.flush()
        record_by(
            db,
            actor,
            "expenses.category.create",
            entity_type="expense_category",
            entity_id=str(row.id),
            changes=diff_changes({}, _category_values(row)),
        )
        return row.id
    found = db.get(ExpenseCategory, category_id)
    if found is None:
        raise NotFound()
    before = _category_values(found)
    found.name, found.gl_account, found.claim_limit = name, account, limit
    found.receipt_above, found.is_active = receipt, is_active
    record_by(
        db,
        actor,
        "expenses.category.update",
        entity_type="expense_category",
        entity_id=str(found.id),
        changes=diff_changes(before, _category_values(found)),
    )
    return found.id


# ------------------------------------------------------------------ spending limits


def limits_for(db: Session, employee_id: uuid.UUID) -> tuple[Decimal | None, Decimal | None]:
    """(weekly, monthly) for one person: their own limits where set, else the defaults."""
    defaults = get_settings(db)
    own = db.scalars(select(PersonLimit).where(PersonLimit.employee_id == employee_id)).first()
    if own is None:
        return defaults.weekly_limit, defaults.monthly_limit
    return (
        own.weekly_limit if own.weekly_limit is not None else defaults.weekly_limit,
        own.monthly_limit if own.monthly_limit is not None else defaults.monthly_limit,
    )


def person_limits(db: Session) -> list[tuple[uuid.UUID, str, Decimal | None, Decimal | None]]:
    rows = list(db.scalars(select(PersonLimit)))
    names = people.names(db, {r.employee_id for r in rows})
    return sorted(
        (
            (r.employee_id, names.get(r.employee_id, ""), r.weekly_limit, r.monthly_limit)
            for r in rows
        ),
        key=lambda r: r[1],
    )


def save_person_limit(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID | None,
    *,
    weekly_limit: str,
    monthly_limit: str,
) -> None:
    """Set one person's limits; both blank removes them (the defaults apply)."""
    _require(db, actor)
    if employee_id is None or people.status_and_account(db, employee_id) is None:
        raise ValidationFailed(field_errors={"limit_employee_id": "Choose a person."})
    weekly = parse_money(weekly_limit, "person_weekly", blank=True)
    monthly = parse_money(monthly_limit, "person_monthly", blank=True)
    row = db.scalars(select(PersonLimit).where(PersonLimit.employee_id == employee_id)).first()
    before = {"weekly": str(row.weekly_limit), "monthly": str(row.monthly_limit)} if row else {}
    if weekly is None and monthly is None:
        if row is not None:
            db.delete(row)
        after: dict[str, object] = {}
    else:
        for name, value in (("person_weekly", weekly), ("person_monthly", monthly)):
            if value is not None and value <= 0:
                raise ValidationFailed(field_errors={name: "Enter an amount above zero."})
        if row is None:
            row = PersonLimit(employee_id=employee_id)
            db.add(row)
        row.weekly_limit, row.monthly_limit = weekly, monthly
        after = {"weekly": str(weekly), "monthly": str(monthly)}
    db.flush()
    record_by(
        db,
        actor,
        "expenses.limit.update",
        entity_type="employee",
        entity_id=str(employee_id),
        changes=diff_changes(before, after),
    )
