"""Leave (ADR 0018): types with entitlement rules, public holidays, requests with their
approval decisions, and balance adjustments (including carry-over)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

COUNTING = ("working", "calendar")
ACCRUALS = ("yearly", "monthly")
REQUEST_STATUSES = ("pending", "approved", "rejected", "cancelled")
STEPS = ("manager", "hr")
DECISIONS = ("approved", "rejected")
ADJUSTMENT_KINDS = ("adjustment", "carry_over")

Days = Numeric(5, 1)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class LeaveType(IdMixin, Base):
    __tablename__ = "leave_types"
    __table_args__ = (
        CheckConstraint(_in("counting", COUNTING), name="counting"),
        CheckConstraint(_in("accrual", ACCRUALS), name="accrual"),
        CheckConstraint("carry_over_max_days >= 0", name="carry_over"),
        CheckConstraint("carry_over_expiry_month BETWEEN 1 AND 12", name="expiry_month"),
        CheckConstraint(
            "certificate_after_days IS NULL OR certificate_after_days >= 0", name="cert"
        ),
    )

    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    paid: Mapped[bool] = mapped_column(Boolean, server_default="true")
    counting: Mapped[str] = mapped_column(String(10), server_default="working")
    has_balance: Mapped[bool] = mapped_column(Boolean, server_default="true")
    accrual: Mapped[str] = mapped_column(String(10), server_default="yearly")
    carry_over_max_days: Mapped[Decimal] = mapped_column(Days, server_default="0")
    carry_over_expiry_month: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    requires_hr_approval: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # A certificate is needed when a request is longer than this many days.
    certificate_after_days: Mapped[Decimal | None] = mapped_column(Days)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class EntitlementRule(IdMixin, Base):
    __tablename__ = "leave_entitlement_rules"
    __table_args__ = (
        CheckConstraint("min_service_months >= 0", name="service"),
        CheckConstraint("days >= 0", name="days"),
    )

    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_types.id", ondelete="CASCADE"), index=True
    )
    grade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("grades.id"))
    min_service_months: Mapped[int] = mapped_column(Integer, server_default="0")
    days: Mapped[Decimal] = mapped_column(Days)


class PublicHoliday(IdMixin, Base):
    __tablename__ = "public_holidays"

    day: Mapped[date] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(120))


class LeaveRequest(IdMixin, Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint(_in("step", STEPS), name="step"),
        CheckConstraint("start_date <= end_date", name="dates"),
        CheckConstraint("days > 0", name="days"),
        Index("ix_leave_requests_dates", "start_date", "end_date"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave_types.id"))
    start_date: Mapped[date]
    end_date: Mapped[date]
    start_half: Mapped[bool] = mapped_column(Boolean, server_default="false")  # from noon
    end_half: Mapped[bool] = mapped_column(Boolean, server_default="false")  # until noon
    days: Mapped[Decimal] = mapped_column(Days)
    year: Mapped[int] = mapped_column(SmallInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    step: Mapped[str] = mapped_column(String(8), server_default="manager")
    # The manager from the org resolver when the request was made.
    manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    certificate_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    __authz__: ClassVar[AuthzColumns]


LeaveRequest.__authz__ = AuthzColumns(owner_employee=LeaveRequest.employee_id)


class LeaveDecision(IdMixin, Base):
    __tablename__ = "leave_decisions"
    __table_args__ = (
        CheckConstraint(_in("step", STEPS), name="step"),
        CheckConstraint(_in("decision", DECISIONS), name="decision"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(8))
    decision: Mapped[str] = mapped_column(String(10))
    decided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)


class LeaveAdjustment(IdMixin, Base):
    __tablename__ = "leave_adjustments"
    __table_args__ = (
        CheckConstraint(_in("kind", ADJUSTMENT_KINDS), name="kind"),
        CheckConstraint("days <> 0", name="days"),
        Index("ix_leave_adjustments_lookup", "employee_id", "leave_type_id", "year"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave_types.id"))
    year: Mapped[int] = mapped_column(SmallInteger)
    days: Mapped[Decimal] = mapped_column(Days)
    kind: Mapped[str] = mapped_column(String(12), server_default="adjustment")
    expires_on: Mapped[date | None]  # carry-over only
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# One carry-over row per person, type and year (the year-start job is safe to re-run).
Index(
    "uq_leave_carry_over",
    LeaveAdjustment.employee_id,
    LeaveAdjustment.leave_type_id,
    LeaveAdjustment.year,
    unique=True,
    postgresql_where=LeaveAdjustment.kind == "carry_over",
)
