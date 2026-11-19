"""Leave settings and balance administration (ADR 0018): types and entitlement rules,
public holidays, HR adjustments, the year-start carry-over, and the balances list."""

import calendar
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz import service as authz
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.leave import balances
from app.modules.leave.models import (
    ACCRUALS,
    COUNTING,
    EntitlementRule,
    LeaveAdjustment,
    LeaveType,
    PublicHoliday,
)
from app.modules.leave.schemas import TypeView
from app.org.models import Grade

MIN_REASON = 5
MAX_DAYS = Decimal(366)
PAGE_SIZE = 50


def _decimal(
    raw: str, field: str, errors: dict[str, str], *, allow_blank: bool = False
) -> Decimal | None:
    value = raw.strip()
    if not value:
        if allow_blank:
            return None
        errors[field] = "Enter a number of days."
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        errors[field] = "Enter a number of days."
        return None
    if number < 0 or number > MAX_DAYS or (number * 2) % 1:
        errors[field] = "Use whole or half days, up to 366."
        return None
    return number


# ------------------------------------------------------------------ types


def list_types(db: Session) -> list[TypeView]:
    grades = {g.id: g.name for g in db.scalars(select(Grade))}
    rules: dict[uuid.UUID, list[tuple[uuid.UUID | None, str, int, Decimal]]] = {}
    for r in db.scalars(select(EntitlementRule).order_by(EntitlementRule.min_service_months)):
        rules.setdefault(r.leave_type_id, []).append(
            (
                r.grade_id,
                grades.get(r.grade_id, "") if r.grade_id else "Any grade",
                r.min_service_months,
                r.days,
            )
        )
    return [
        TypeView(
            id=t.id,
            code=t.code,
            name=t.name,
            paid=t.paid,
            counting=t.counting,
            has_balance=t.has_balance,
            accrual=t.accrual,
            carry_over_max_days=t.carry_over_max_days,
            carry_over_expiry_month=t.carry_over_expiry_month,
            requires_hr_approval=t.requires_hr_approval,
            certificate_after_days=t.certificate_after_days,
            is_active=t.is_active,
            rules=rules.get(t.id, []),
        )
        for t in db.scalars(select(LeaveType).order_by(LeaveType.sort_order, LeaveType.name))
    ]


def save_type(
    db: Session,
    actor: Actor,
    type_id: uuid.UUID | None,
    *,
    name: str,
    paid: bool,
    counting: str,
    has_balance: bool,
    accrual: str,
    carry_over_max_days: str,
    carry_over_expiry_month: str,
    requires_hr_approval: bool,
    certificate_after_days: str,
    is_active: bool,
    rules: list[tuple[str, str, str]],  # (grade id, months, days)
) -> uuid.UUID:
    authz.require(db, actor, "leave.settings.manage")
    errors: dict[str, str] = {}
    name = " ".join(name.split())
    if not 2 <= len(name) <= 80:
        errors["name"] = "Give the type a name."
    if counting not in COUNTING:
        errors["counting"] = "Choose how days are counted."
    if accrual not in ACCRUALS:
        errors["accrual"] = "Choose how days build up."
    carry = _decimal(carry_over_max_days or "0", "carry_over_max_days", errors)
    month = int(carry_over_expiry_month) if carry_over_expiry_month.isdigit() else 0
    if not 1 <= month <= 12:
        errors["carry_over_expiry_month"] = "Choose a month."
    certificate = _decimal(
        certificate_after_days, "certificate_after_days", errors, allow_blank=True
    )
    cleaned_rules = _clean_rules(db, rules, errors)
    taken = db.scalar(select(LeaveType.id).where(func.lower(LeaveType.name) == name.lower()))
    if taken is not None and taken != type_id:
        errors["name"] = "There is already a type with this name."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if type_id is None:
        code = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "leave"
        if db.scalar(select(LeaveType.id).where(LeaveType.code == code)):
            code = f"{code[:21]}_{uuid.uuid4().hex[:8]}"
        row = LeaveType(code=code, name=name, sort_order=100)
        db.add(row)
        before: dict[str, object] = {}
    else:
        found = db.get(LeaveType, type_id)
        if found is None:
            raise NotFound()
        row = found
        before = _type_facts(row)
    row.name, row.paid, row.counting, row.has_balance = name, paid, counting, has_balance
    row.accrual, row.carry_over_max_days = accrual, carry or Decimal(0)
    row.carry_over_expiry_month, row.requires_hr_approval = month, requires_hr_approval
    row.certificate_after_days, row.is_active = certificate, is_active
    db.flush()
    for old in db.scalars(select(EntitlementRule).where(EntitlementRule.leave_type_id == row.id)):
        db.delete(old)
    for grade_id, months, days in cleaned_rules:
        db.add(
            EntitlementRule(
                leave_type_id=row.id, grade_id=grade_id, min_service_months=months, days=days
            )
        )
    db.flush()
    record_by(
        db,
        actor,
        "leave.type.save",
        entity_type="leave_type",
        entity_id=str(row.id),
        changes=diff_changes(before, {**_type_facts(row), "rules": len(cleaned_rules)}),
    )
    return row.id


def _type_facts(row: LeaveType) -> dict[str, object]:
    return {
        "name": row.name,
        "paid": row.paid,
        "counting": row.counting,
        "has_balance": row.has_balance,
        "accrual": row.accrual,
        "carry_over_max_days": str(row.carry_over_max_days),
        "carry_over_expiry_month": row.carry_over_expiry_month,
        "requires_hr_approval": row.requires_hr_approval,
        "certificate_after_days": str(row.certificate_after_days)
        if row.certificate_after_days is not None
        else None,
        "is_active": row.is_active,
    }


def _clean_rules(
    db: Session, rules: list[tuple[str, str, str]], errors: dict[str, str]
) -> list[tuple[uuid.UUID | None, int, Decimal]]:
    cleaned: list[tuple[uuid.UUID | None, int, Decimal]] = []
    seen: set[tuple[uuid.UUID | None, int]] = set()
    for i, (grade, months, days) in enumerate(rules):
        if not days.strip():
            continue
        try:
            grade_id = uuid.UUID(grade) if grade else None
        except ValueError:
            grade_id = None
        if grade_id is not None and db.get(Grade, grade_id) is None:
            errors[f"rule_{i}"] = "Choose a grade."
            continue
        if not months.strip().isdigit() or int(months) > 600:
            errors[f"rule_{i}"] = "Months of service must be a whole number."
            continue
        amount = _decimal(days, f"rule_{i}", errors)
        if amount is None:
            continue
        key = (grade_id, int(months))
        if key in seen:
            errors[f"rule_{i}"] = "This rule is listed twice."
            continue
        seen.add(key)
        cleaned.append((grade_id, int(months), amount))
    return cleaned


# ------------------------------------------------------------------ public holidays


def holidays(db: Session, year: int) -> list[PublicHoliday]:
    return list(
        db.scalars(
            select(PublicHoliday)
            .where(PublicHoliday.day >= date(year, 1, 1), PublicHoliday.day <= date(year, 12, 31))
            .order_by(PublicHoliday.day)
        )
    )


def add_holiday(db: Session, actor: Actor, *, day: date | None, name: str) -> None:
    authz.require(db, actor, "leave.settings.manage")
    name = " ".join(name.split())
    errors = {}
    if day is None:
        errors["day"] = "Choose the date."
    if not 2 <= len(name) <= 120:
        errors["holiday_name"] = "Name the holiday."
    if day is not None and db.scalar(select(PublicHoliday.id).where(PublicHoliday.day == day)):
        errors["day"] = "There is already a holiday on this date."
    if errors or day is None:
        raise ValidationFailed(field_errors=errors)
    # Requests already booked over the new holiday keep their day count; HR adjusts
    # balances if needed.
    db.add(PublicHoliday(day=day, name=name))
    db.flush()
    record_by(
        db,
        actor,
        "leave.holiday.add",
        entity_type="public_holiday",
        entity_id=day.isoformat(),
        changes={"name": {"from": None, "to": name}},
    )


def delete_holiday(db: Session, actor: Actor, holiday_id: uuid.UUID) -> None:
    authz.require(db, actor, "leave.settings.manage")
    row = db.get(PublicHoliday, holiday_id)
    if row is None:
        raise NotFound()
    record_by(
        db,
        actor,
        "leave.holiday.delete",
        entity_type="public_holiday",
        entity_id=row.day.isoformat(),
        changes={"name": {"from": row.name, "to": None}},
    )
    db.delete(row)


# ------------------------------------------------------------------ balances


def adjust(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    leave_type_id: uuid.UUID | None,
    year: int,
    days: str,
    reason: str,
) -> None:
    resource = Resource(owner_employee_id=employee_id)
    if not engine.check(db, actor.user_id, "leave.balance.adjust", resource):
        raise PermissionDenied()
    facts = people.status_and_account(db, employee_id)
    if facts is None:
        raise NotFound()
    owner = facts[1]
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    broken = sod.violations(
        catalogue.sod_rules(),
        "leave.balance.adjust",
        actor.user_id,
        {"subject_user_id": owner},
        held,
    )
    if broken:
        raise PermissionDenied(broken[0].message)
    leave_type = db.get(LeaveType, leave_type_id) if leave_type_id else None
    errors: dict[str, str] = {}
    if leave_type is None or not leave_type.has_balance:
        errors["leave_type_id"] = "Choose a type of leave with a balance."
    value = days.strip().replace("+", "")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        amount = Decimal(0)
    if amount == 0 or (amount * 2) % 1 or abs(amount) > MAX_DAYS:
        errors["days"] = "Enter whole or half days to add (or remove with a minus)."
    reason = reason.strip()
    if len(reason) < MIN_REASON:
        errors["reason"] = "Say why, in a few words."
    if errors or leave_type is None:
        raise ValidationFailed(field_errors=errors)
    db.add(
        LeaveAdjustment(
            employee_id=employee_id,
            leave_type_id=leave_type.id,
            year=year,
            days=amount,
            reason=reason,
            created_by=actor.user_id,
        )
    )
    record_by(
        db,
        actor,
        "leave.balance.adjust",
        entity_type="employee",
        entity_id=str(employee_id),
        reason=reason,
        changes={"days": {"from": None, "to": f"{amount:+} {leave_type.name} {year}"}},
    )


def adjustments(
    db: Session, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year: int
) -> list[LeaveAdjustment]:
    return list(
        db.scalars(
            select(LeaveAdjustment)
            .where(
                LeaveAdjustment.employee_id == employee_id,
                LeaveAdjustment.leave_type_id == leave_type_id,
                LeaveAdjustment.year == year,
            )
            .order_by(LeaveAdjustment.created_at)
        )
    )


def balance_page(
    db: Session, actor: Actor, *, year: int, q: str = "", page: int = 1
) -> tuple[list[Any], dict[uuid.UUID, list[balances.Balance]], int]:
    """People in the actor's balance scope with their balances for the year."""
    rows, pages = people.page_in_scope(
        db, actor, "leave.balance.read", q=q, page=page, size=PAGE_SIZE
    )
    types = list(
        db.scalars(
            select(LeaveType)
            .where(LeaveType.is_active, LeaveType.has_balance)
            .order_by(LeaveType.sort_order)
        )
    )
    return rows, balances.balances(db, [r.id for r in rows], year, types=types), pages


def carry_over(db: Session, year: int | None = None) -> int:
    """Write carry-over into `year` (default: this year) from last year's unused days,
    up to each type's cap. Safe to run again: existing rows are kept."""
    year = year or clock.today().year
    types = [
        t
        for t in db.scalars(select(LeaveType).where(LeaveType.is_active, LeaveType.has_balance))
        if t.carry_over_max_days > 0
    ]
    if not types:
        return 0
    employees = people.current_ids(db)
    done = {
        (a.employee_id, a.leave_type_id)
        for a in db.scalars(
            select(LeaveAdjustment).where(
                LeaveAdjustment.year == year, LeaveAdjustment.kind == "carry_over"
            )
        )
    }
    previous = balances.balances(db, employees, year - 1, types=types)
    written = 0
    for eid, rows in previous.items():
        for b in rows:
            t = next(t for t in types if t.id == b.leave_type_id)
            unused = b.entitlement + b.adjustments - b.taken  # last year's carry-over expired
            amount = min(t.carry_over_max_days, max(unused, Decimal(0)))
            if amount <= 0 or (eid, t.id) in done:
                continue
            month = t.carry_over_expiry_month
            db.add(
                LeaveAdjustment(
                    employee_id=eid,
                    leave_type_id=t.id,
                    year=year,
                    days=amount,
                    kind="carry_over",
                    expires_on=date(year, month, calendar.monthrange(year, month)[1]),
                    reason=f"Carried over from {year - 1}",
                )
            )
            written += 1
    if written:
        record_by(db, None, "leave.carry_over", entity_type="leave", entity_id=str(year))
    return written
