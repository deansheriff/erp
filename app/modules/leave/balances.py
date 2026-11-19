"""Entitlements and balances (ADR 0018). Balance = entitlement + unexpired carry-over +
adjustments - approved - pending, per person, type and calendar year. Everything is
loaded in bulk so lists of many people stay to a handful of queries."""

import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.modules.hr import service as people
from app.modules.leave.models import EntitlementRule, LeaveAdjustment, LeaveRequest, LeaveType
from app.org import service as org

ZERO = Decimal(0)
HALF = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class Balance:
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    type_name: str
    year: int
    has_balance: bool
    entitlement: Decimal
    carried_over: Decimal
    carry_over_expires: date | None
    adjustments: Decimal
    taken: Decimal
    pending: Decimal

    @property
    def available(self) -> Decimal:
        return self.entitlement + self.carried_over + self.adjustments - self.taken - self.pending


def _half_floor(value: Decimal) -> Decimal:
    """Round down to a half day."""
    return (value / HALF).to_integral_value(rounding=ROUND_FLOOR) * HALF


def _add_months(day: date, months: int) -> date:
    years, month_index = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _months_left(eligible: date) -> int:
    """Whole months of the year left once someone becomes eligible on `eligible`."""
    return 12 - eligible.month + (1 if eligible.day == 1 else 0)


def _rules_for(rules: list[EntitlementRule], grade_id: uuid.UUID | None) -> list[EntitlementRule]:
    """Rules for the person's grade if any exist, otherwise the "any grade" rules."""
    own = [r for r in rules if grade_id is not None and r.grade_id == grade_id]
    return own or [r for r in rules if r.grade_id is None]


def entitlement(
    leave_type: LeaveType,
    rules: list[EntitlementRule],
    *,
    hire_date: date | None,
    grade_id: uuid.UUID | None,
    year: int,
    today: date,
) -> Decimal:
    """Days a person is entitled to in a year (confirm the pro-rating rules)."""
    if not leave_type.has_balance:
        return ZERO
    hired = hire_date or date(year, 1, 1)  # no hire date: treated as hired before the year
    best = ZERO
    for rule in _rules_for(rules, grade_id):
        eligible = _add_months(hired, rule.min_service_months)
        if eligible.year > year:
            continue
        if leave_type.accrual == "monthly":
            last_month = 12 if today.year > year else (today.month if today.year == year else 0)
            first_month = 1 if eligible.year < year else eligible.month
            months = max(0, last_month - first_month + 1)
            amount = rule.days * months / 12
        elif eligible.year < year or (eligible.month == 1 and eligible.day == 1):
            amount = rule.days
        else:
            amount = rule.days * _months_left(eligible) / 12
        best = max(best, _half_floor(Decimal(amount)))
    return best


PairKey = tuple[uuid.UUID, uuid.UUID]


def _sums(
    db: Session, employee_ids: list[uuid.UUID], year: int
) -> tuple[
    dict[tuple[uuid.UUID, uuid.UUID, str], Decimal],
    dict[PairKey, Decimal],
    dict[PairKey, tuple[Decimal, date | None]],
]:
    requests: dict[tuple[uuid.UUID, uuid.UUID, str], Decimal] = defaultdict(lambda: ZERO)
    for eid, tid, status, days in db.execute(
        select(
            LeaveRequest.employee_id,
            LeaveRequest.leave_type_id,
            LeaveRequest.status,
            func.sum(LeaveRequest.days),
        )
        .where(
            LeaveRequest.employee_id.in_(employee_ids),
            LeaveRequest.year == year,
            LeaveRequest.status.in_(("pending", "approved")),
        )
        .group_by(LeaveRequest.employee_id, LeaveRequest.leave_type_id, LeaveRequest.status)
    ).all():
        requests[(eid, tid, status)] = Decimal(days)
    adjustments: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = defaultdict(lambda: ZERO)
    carry: dict[tuple[uuid.UUID, uuid.UUID], tuple[Decimal, date | None]] = {}
    today = clock.today()
    for row in db.scalars(
        select(LeaveAdjustment).where(
            LeaveAdjustment.employee_id.in_(employee_ids), LeaveAdjustment.year == year
        )
    ):
        key = (row.employee_id, row.leave_type_id)
        if row.kind == "carry_over":
            expired = row.expires_on is not None and row.expires_on < today
            carry[key] = (ZERO if expired else row.days, row.expires_on)
        else:
            adjustments[key] += row.days
    return requests, adjustments, carry


def balances(
    db: Session,
    employee_ids: list[uuid.UUID],
    year: int,
    *,
    types: list[LeaveType] | None = None,
) -> dict[uuid.UUID, list[Balance]]:
    """Balances for many people at once, for every active type (or `types`)."""
    if not employee_ids:
        return {}
    types = (
        types
        if types is not None
        else list(
            db.scalars(select(LeaveType).where(LeaveType.is_active).order_by(LeaveType.sort_order))
        )
    )
    rules: dict[uuid.UUID, list[EntitlementRule]] = defaultdict(list)
    for rule in db.scalars(select(EntitlementRule)):
        rules[rule.leave_type_id].append(rule)
    hires = people.hire_dates(db, employee_ids)
    grades = org.grades_of(db, employee_ids)
    requests, adjustments, carry = _sums(db, employee_ids, year)
    today = clock.today()
    result: dict[uuid.UUID, list[Balance]] = {}
    for eid in employee_ids:
        result[eid] = []
        for t in types:
            carried, expires = carry.get((eid, t.id), (ZERO, None))
            result[eid].append(
                Balance(
                    employee_id=eid,
                    leave_type_id=t.id,
                    type_name=t.name,
                    year=year,
                    has_balance=t.has_balance,
                    entitlement=entitlement(
                        t,
                        rules[t.id],
                        hire_date=hires.get(eid),
                        grade_id=grades.get(eid),
                        year=year,
                        today=today,
                    ),
                    carried_over=carried,
                    carry_over_expires=expires,
                    adjustments=adjustments[(eid, t.id)],
                    taken=requests[(eid, t.id, "approved")],
                    pending=requests[(eid, t.id, "pending")],
                )
            )
    return result


def balance(db: Session, employee_id: uuid.UUID, leave_type: LeaveType, year: int) -> Balance:
    return balances(db, [employee_id], year, types=[leave_type])[employee_id][0]
