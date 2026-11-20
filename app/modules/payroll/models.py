"""Payroll (ADR 0021): pay groups, components and salary structures, people's pay and
recurring deductions, effective-dated statutory settings, rent relief declarations, and
runs with their payslips."""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin

KINDS = ("earning", "deduction")
CALCULATIONS = ("fixed", "percent")
RUN_STATUSES = ("draft", "calculated", "reviewed", "approved", "posted", "paid")
DECLARATION_STATUSES = ("pending", "verified", "rejected")

Money = Numeric(14, 2)
Rate = Numeric(7, 4)  # 0.0800 = 8%


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PayGroup(IdMixin, Base):
    __tablename__ = "pay_groups"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class PayComponent(IdMixin, Base):
    __tablename__ = "pay_components"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="kind"),
        CheckConstraint(_in("calculation", CALCULATIONS), name="calculation"),
        CheckConstraint(
            "calculation = 'fixed' OR (percent > 0 AND base_component_id IS NOT NULL)",
            name="percent",
        ),
        CheckConstraint("kind = 'earning' OR NOT pensionable", name="pensionable"),
    )

    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(10))
    calculation: Mapped[str] = mapped_column(String(8), server_default="fixed")
    percent: Mapped[Decimal | None] = mapped_column(Rate)
    base_component_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pay_components.id"))
    taxable: Mapped[bool] = mapped_column(Boolean, server_default="true")
    pensionable: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Built-in components the calculation relies on (basic, overtime) can't be removed.
    system: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class SalaryStructure(IdMixin, Base):
    __tablename__ = "salary_structures"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class StructureComponent(IdMixin, Base):
    __tablename__ = "salary_structure_components"
    __table_args__ = (UniqueConstraint("structure_id", "component_id"),)

    structure_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("salary_structures.id", ondelete="CASCADE")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pay_components.id"))
    default_amount: Mapped[Decimal | None] = mapped_column(Money)


class EmployeePay(IdMixin, Base):
    """A person's pay from a date: group, structure and the amount of each component."""

    __tablename__ = "employee_pay"
    __table_args__ = (CheckConstraint("end_date IS NULL OR start_date <= end_date", name="dates"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    pay_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pay_groups.id"))
    structure_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("salary_structures.id"))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __authz__: ClassVar[AuthzColumns]


EmployeePay.__authz__ = AuthzColumns(owner_employee=EmployeePay.employee_id)


class EmployeePayAmount(IdMixin, Base):
    __tablename__ = "employee_pay_amounts"
    __table_args__ = (
        UniqueConstraint("pay_id", "component_id"),
        CheckConstraint("amount >= 0", name="amount"),
    )

    pay_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee_pay.id", ondelete="CASCADE"))
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pay_components.id"))
    amount: Mapped[Decimal] = mapped_column(Money)  # monthly; unused for percent components


class EmployeeDeduction(IdMixin, Base):
    """A recurring monthly deduction, e.g. cooperative savings or union dues."""

    __tablename__ = "employee_deductions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("end_month IS NULL OR start_month <= end_month", name="months"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pay_components.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    start_month: Mapped[date]  # first day of the month
    end_month: Mapped[date | None]
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class StatutorySet(IdMixin, Base):
    """Statutory rates in force from a date (editable, confirm before go-live)."""

    __tablename__ = "statutory_sets"
    __table_args__ = (
        CheckConstraint(
            "pension_employee_rate >= 0 AND pension_employer_rate >= 0 AND nhf_rate >= 0 "
            "AND nhis_rate >= 0 AND nsitf_rate >= 0 AND itf_rate >= 0 "
            "AND rent_relief_rate >= 0 AND overtime_multiplier >= 0",
            name="rates",
        ),
        CheckConstraint("hours_per_month > 0", name="hours"),
    )

    effective_from: Mapped[date] = mapped_column(unique=True)
    label: Mapped[str] = mapped_column(String(120))
    rent_relief_rate: Mapped[Decimal] = mapped_column(Rate)
    rent_relief_cap: Mapped[Decimal] = mapped_column(Money)
    pension_employee_rate: Mapped[Decimal] = mapped_column(Rate)
    pension_employer_rate: Mapped[Decimal] = mapped_column(Rate)
    nhf_rate: Mapped[Decimal] = mapped_column(Rate)
    nhis_rate: Mapped[Decimal] = mapped_column(Rate)
    nsitf_rate: Mapped[Decimal] = mapped_column(Rate)
    itf_rate: Mapped[Decimal] = mapped_column(Rate)
    minimum_wage: Mapped[Decimal] = mapped_column(Money)
    overtime_multiplier: Mapped[Decimal] = mapped_column(Rate)
    hours_per_month: Mapped[int] = mapped_column(SmallInteger)


class TaxBand(IdMixin, Base):
    """One PAYE band: the next `width` of annual taxable income (None: the rest)."""

    __tablename__ = "tax_bands"
    __table_args__ = (
        UniqueConstraint("set_id", "position"),
        CheckConstraint("width IS NULL OR width > 0", name="width"),
        CheckConstraint("rate >= 0 AND rate <= 1", name="rate"),
    )

    set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("statutory_sets.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    width: Mapped[Decimal | None] = mapped_column(Money)
    rate: Mapped[Decimal] = mapped_column(Rate)


class RentDeclaration(IdMixin, Base):
    __tablename__ = "rent_declarations"
    __table_args__ = (
        CheckConstraint(_in("status", DECLARATION_STATUSES), name="status"),
        CheckConstraint("annual_rent > 0", name="rent"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    tax_year: Mapped[int] = mapped_column(SmallInteger)
    annual_rent: Mapped[Decimal] = mapped_column(Money)
    proof_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    declared_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


RentDeclaration.__authz__ = AuthzColumns(owner_employee=RentDeclaration.employee_id)

# One open or verified declaration per person and tax year.
Index(
    "uq_rent_declaration_live",
    RentDeclaration.employee_id,
    RentDeclaration.tax_year,
    unique=True,
    postgresql_where=RentDeclaration.status != "rejected",
)


class PayrollRun(IdMixin, Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (
        UniqueConstraint("pay_group_id", "year", "month"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month"),
    )

    pay_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pay_groups.id"))
    year: Mapped[int] = mapped_column(SmallInteger)
    month: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(10), server_default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    calculated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    calculated_at: Mapped[datetime | None]
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    paid_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    paid_at: Mapped[datetime | None]
    returned_note: Mapped[str | None] = mapped_column(Text)
    statutory_set_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("statutory_sets.id"))


class Payslip(IdMixin, Base):
    """One person's pay in a run (amounts for the month, and year to date for PAYE)."""

    __tablename__ = "payslips"
    __table_args__ = (UniqueConstraint("run_id", "employee_id"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    days_in_month: Mapped[int] = mapped_column(SmallInteger)
    days_paid: Mapped[int] = mapped_column(SmallInteger)
    gross: Mapped[Decimal] = mapped_column(Money)
    taxable: Mapped[Decimal] = mapped_column(Money)  # after pension, NHF and NHIS
    paye: Mapped[Decimal] = mapped_column(Money)
    pension_employee: Mapped[Decimal] = mapped_column(Money)
    pension_employer: Mapped[Decimal] = mapped_column(Money)
    nhf: Mapped[Decimal] = mapped_column(Money)
    nhis: Mapped[Decimal] = mapped_column(Money)
    nsitf: Mapped[Decimal] = mapped_column(Money)  # employer
    other_deductions: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    rent_relief: Mapped[Decimal] = mapped_column(Money)  # this month's share
    ytd_taxable: Mapped[Decimal] = mapped_column(Money)
    ytd_paye: Mapped[Decimal] = mapped_column(Money)
    warnings: Mapped[str | None] = mapped_column(Text)  # one per line
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))

    __authz__: ClassVar[AuthzColumns]


Payslip.__authz__ = AuthzColumns(owner_employee=Payslip.employee_id)


class PayslipLine(IdMixin, Base):
    __tablename__ = "payslip_lines"
    __table_args__ = (CheckConstraint(_in("kind", KINDS), name="kind"),)

    payslip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payslips.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Money)
    detail: Mapped[str | None] = mapped_column(String(80))  # e.g. "6 h at 1.5x"
