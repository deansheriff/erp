"""Employees (ADR 0015). Phase 4 holds only what the org chart needs: the `directory`
field class. Personal, banking, statutory and compensation details arrive in Phase 6."""

import uuid
from typing import ClassVar

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.authz.engine import AuthzColumns
from app.core.db import Base, IdMixin, TimestampMixin

EMPLOYEE_STATUSES = ("active", "on_leave", "suspended", "terminated")


class Employee(IdMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_leave', 'suspended', 'terminated')", name="status"
        ),
        CheckConstraint("work_email = lower(work_email)", name="email_lowercase"),
    )

    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    other_names: Mapped[str | None] = mapped_column(String(120))
    work_email: Mapped[str | None] = mapped_column(String(254), unique=True)
    # The sign-in account, when the person has one. One account per employee.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(String(12), server_default="active")

    __authz__: ClassVar[AuthzColumns]

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.other_names, self.last_name) if p)


# An employee record is "owned" by the employee it describes (self scope).
Employee.__authz__ = AuthzColumns(owner_employee=Employee.id)
