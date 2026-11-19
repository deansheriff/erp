"""The HR record (ADR 0015, ADR 0017): employees with classed fields, change requests,
documents, onboarding and offboarding checklists, and exits. Field classes are declared
in fields.py."""

import uuid
from datetime import date, datetime
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin, TimestampMixin

EMPLOYEE_STATUSES = ("active", "on_leave", "suspended", "terminated")
EMPLOYMENT_TYPES = ("permanent", "contract", "intern", "nysc")
GENDERS = ("female", "male")
MARITAL_STATUSES = ("single", "married", "divorced", "widowed", "separated")
REQUEST_CLASSES = ("statutory", "banking")
REQUEST_STATUSES = ("pending", "approved", "rejected", "cancelled")
DOCUMENT_CLASSES = ("personal", "statutory", "banking", "compensation", "sensitive")
CHECKLIST_KINDS = ("onboarding", "offboarding")
ASSIGNEES = ("hr", "it", "manager", "employee")
EXIT_KINDS = ("resignation", "termination")
EXIT_STATUSES = ("scheduled", "revoked", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Employee(IdMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint(_in("status", EMPLOYEE_STATUSES), name="status"),
        CheckConstraint("work_email = lower(work_email)", name="email_lowercase"),
        CheckConstraint(_in("employment_type", EMPLOYMENT_TYPES), name="employment_type"),
        CheckConstraint(_in("gender", GENDERS), name="gender"),
        CheckConstraint(_in("marital_status", MARITAL_STATUSES), name="marital_status"),
        CheckConstraint("nysc_end IS NULL OR nysc_start <= nysc_end", name="nysc_dates"),
    )

    # directory
    employee_number: Mapped[str | None] = mapped_column(String(30), unique=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    other_names: Mapped[str | None] = mapped_column(String(120))
    work_email: Mapped[str | None] = mapped_column(String(254), unique=True)
    # The sign-in account, when the person has one. One account per employee.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(String(12), server_default="active")
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))

    # personal: self-service
    phone: Mapped[str | None] = mapped_column(String(30))
    personal_email: Mapped[str | None] = mapped_column(String(254))
    residential_address: Mapped[str | None] = mapped_column(Text)
    state_of_residence: Mapped[str | None] = mapped_column(String(40))
    marital_status: Mapped[str | None] = mapped_column(String(12))
    next_of_kin_name: Mapped[str | None] = mapped_column(String(160))
    next_of_kin_relationship: Mapped[str | None] = mapped_column(String(60))
    next_of_kin_phone: Mapped[str | None] = mapped_column(String(30))
    next_of_kin_address: Mapped[str | None] = mapped_column(Text)

    # personal: HR-managed
    date_of_birth: Mapped[date | None]
    gender: Mapped[str | None] = mapped_column(String(10))
    state_of_origin: Mapped[str | None] = mapped_column(String(40))
    employment_type: Mapped[str | None] = mapped_column(String(12))
    nysc_start: Mapped[date | None]
    nysc_end: Mapped[date | None]
    hire_date: Mapped[date | None]
    probation_end: Mapped[date | None]
    confirmation_date: Mapped[date | None]

    # statutory (identifiers encrypted with app.core.secrets)
    nin_encrypted: Mapped[str | None] = mapped_column(Text)
    tin_encrypted: Mapped[str | None] = mapped_column(Text)
    pension_pfa: Mapped[str | None] = mapped_column(String(120))
    rsa_pin_encrypted: Mapped[str | None] = mapped_column(Text)
    nhf_number_encrypted: Mapped[str | None] = mapped_column(Text)

    # banking (identifiers encrypted)
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_number_encrypted: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(String(160))
    bvn_encrypted: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.other_names, self.last_name) if p)


# An employee record is "owned" by the employee it describes (self scope).
Employee.__authz__ = AuthzColumns(owner_employee=Employee.id)


class ChangeRequest(IdMixin, Base):
    """An employee's request to change their own statutory or banking details."""

    __tablename__ = "employee_change_requests"
    __table_args__ = (
        CheckConstraint(_in("field_class", REQUEST_CLASSES), name="field_class"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    field_class: Mapped[str] = mapped_column(String(12))
    field_names: Mapped[str] = mapped_column(Text)  # comma-separated, for display
    values_encrypted: Mapped[str] = mapped_column(Text)  # JSON of the new values
    status: Mapped[str] = mapped_column(String(12), server_default="pending")
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


ChangeRequest.__authz__ = AuthzColumns(owner_employee=ChangeRequest.employee_id)
# One open request per class per employee.
Index(
    "uq_change_requests_open",
    ChangeRequest.employee_id,
    ChangeRequest.field_class,
    unique=True,
    postgresql_where=ChangeRequest.status == "pending",
)


class DocumentCategory(IdMixin, Base):
    __tablename__ = "employee_document_categories"
    __table_args__ = (CheckConstraint(_in("field_class", DOCUMENT_CLASSES), name="field_class"),)

    name: Mapped[str] = mapped_column(String(80), unique=True)
    field_class: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class EmployeeDocument(IdMixin, Base):
    """An employee document; its versions are files in one version group."""

    __tablename__ = "employee_documents"

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee_document_categories.id"))
    title: Mapped[str] = mapped_column(String(160))
    field_class: Mapped[str] = mapped_column(String(16))  # the category's class when added
    version_group_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]


class ChecklistTemplate(IdMixin, Base):
    __tablename__ = "checklist_templates"
    __table_args__ = (
        CheckConstraint(_in("kind", CHECKLIST_KINDS), name="kind"),
        CheckConstraint("department_id IS NULL OR position_id IS NULL", name="one_target"),
    )

    kind: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ChecklistTemplateTask(IdMixin, Base):
    __tablename__ = "checklist_template_tasks"
    __table_args__ = (CheckConstraint(_in("assignee", ASSIGNEES), name="assignee"),)

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checklist_templates.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    assignee: Mapped[str] = mapped_column(String(10))
    # Days after the start date (onboarding) or before the last working day
    # (offboarding, as a negative number).
    due_offset_days: Mapped[int] = mapped_column(Integer, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class Checklist(IdMixin, Base):
    __tablename__ = "checklists"
    __table_args__ = (CheckConstraint(_in("kind", CHECKLIST_KINDS), name="kind"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(120))
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("checklist_templates.id"))
    exit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee_exits.id"))
    anchor_date: Mapped[date]  # start date, or last working day
    # The manager on the day the checklist started (from the org resolver).
    manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    started_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]


class ChecklistTask(IdMixin, Base):
    __tablename__ = "checklist_tasks"
    __table_args__ = (CheckConstraint(_in("assignee", ASSIGNEES), name="assignee"),)

    checklist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checklists.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    assignee: Mapped[str] = mapped_column(String(10))
    due_date: Mapped[date]
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    done_at: Mapped[datetime | None]
    done_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class EmployeeExit(IdMixin, Base):
    __tablename__ = "employee_exits"
    __table_args__ = (
        CheckConstraint(_in("kind", EXIT_KINDS), name="kind"),
        CheckConstraint(_in("status", EXIT_STATUSES), name="status"),
        CheckConstraint("notice_date <= last_working_day", name="dates"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(Text)
    notice_date: Mapped[date]
    last_working_day: Mapped[date]
    final_pay_due: Mapped[bool] = mapped_column(Boolean, server_default="true")
    status: Mapped[str] = mapped_column(String(10), server_default="scheduled")
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


# One live (scheduled or revoked) exit per employee.
Index(
    "uq_employee_exits_live",
    EmployeeExit.employee_id,
    unique=True,
    postgresql_where=EmployeeExit.status != "cancelled",
)
