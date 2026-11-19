"""The organisation this installation serves (ADR 0002) and its structure (ADR 0015)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class Organization(IdMixin, Base):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="fiscal_month"),
    )

    # Always true and unique, so a second row can never be inserted.
    is_singleton: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), unique=True
    )
    legal_name: Mapped[str] = mapped_column(String(200))
    trading_name: Mapped[str | None] = mapped_column(String(200))
    rc_number: Mapped[str | None] = mapped_column(String(30))
    tin: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), server_default="Africa/Lagos")
    base_currency: Mapped[str] = mapped_column(String(3), server_default="NGN")
    fiscal_year_start_month: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    # Colours used on generated documents (letterheads, payslips, invoices).
    brand_primary: Mapped[str | None] = mapped_column(String(7))
    brand_accent: Mapped[str | None] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ------------------------------------------------------------------ structure (Phase 4, ADR 0015)


class ReferenceMixin(IdMixin, TimestampMixin):
    """Shared shape of branches, cost centres and grades."""

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Branch(ReferenceMixin, Base):
    __tablename__ = "branches"

    address: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(60))


class CostCentre(ReferenceMixin, Base):
    __tablename__ = "cost_centres"


class Grade(ReferenceMixin, Base):
    __tablename__ = "grades"

    rank: Mapped[int] = mapped_column(SmallInteger)  # higher is more senior


class Department(IdMixin, TimestampMixin, Base):
    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_departments_parent_id", "parent_id"),
    )

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Position(IdMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_positions_parent_id", "parent_id"),
        Index("ix_positions_department_id", "department_id"),
    )

    title: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    grade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("grades.id"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


ASSIGNMENT_KINDS = ("primary", "acting", "interim")


class PositionAssignment(IdMixin, Base):
    """An employee in a position for a period. `end_date` is inclusive; None is open."""

    __tablename__ = "position_assignments"
    __table_args__ = (
        CheckConstraint("kind IN ('primary', 'acting', 'interim')", name="kind"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="period"),
        Index("ix_position_assignments_position_id", "position_id"),
        Index("ix_position_assignments_employee_id", "employee_id"),
    )

    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"))
    # The employees table belongs to the HR module; only its key is known here.
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    kind: Mapped[str] = mapped_column(String(10))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrgState(IdMixin, Base):
    """A counter bumped by every structural change; resolver snapshots are keyed on it."""

    __tablename__ = "org_state"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        UniqueConstraint("is_singleton", name="uq_org_state_is_singleton"),
    )

    is_singleton: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    version: Mapped[int] = mapped_column(BigInteger, server_default="1")
