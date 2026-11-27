"""Projects (ADR 0031): projects with a client, manager, budget by category and members;
milestones and tasks; weekly timesheets approved by the project's manager; hourly cost
rates; budget alerts. Revenue and costs live on the ledger (journal lines name the
project)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

Money = Numeric(16, 2)
STATUSES = ("planned", "active", "on_hold", "completed", "cancelled")
CATEGORIES = ("labour", "materials", "expenses", "other")
TASK_STATUSES = ("todo", "in_progress", "blocked", "done")
SHEET_STATUSES = ("draft", "submitted", "approved", "rejected")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Project(IdMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
    )

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    manager_employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="planned")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BudgetLine(IdMixin, Base):
    __tablename__ = "project_budgets"
    __table_args__ = (
        CheckConstraint(_in("category", CATEGORIES), name="category"),
        CheckConstraint("amount >= 0", name="amount"),
        UniqueConstraint("project_id", "category"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Money)


class Member(IdMixin, Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "employee_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)


class Milestone(IdMixin, Base):
    __tablename__ = "project_milestones"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date | None]
    done_at: Mapped[datetime | None]


class Task(IdMixin, Base):
    __tablename__ = "project_tasks"
    __table_args__ = (CheckConstraint(_in("status", TASK_STATUSES), name="status"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_milestones.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    assignee_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(11), server_default="todo")
    due_date: Mapped[date | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Timesheet(IdMixin, Base):
    """One person's hours on one project for a week starting Monday."""

    __tablename__ = "timesheets"
    __table_args__ = (
        CheckConstraint(_in("status", SHEET_STATUSES), name="status"),
        CheckConstraint("extract(isodow from week_start) = 1", name="monday"),
        UniqueConstraint("project_id", "employee_id", "week_start"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    week_start: Mapped[date]
    status: Mapped[str] = mapped_column(String(9), server_default="draft")
    # Who it went to: the manager, or the manager's line manager for the manager's own time.
    approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    submitted_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)


class TimeEntry(IdMixin, Base):
    __tablename__ = "time_entries"
    __table_args__ = (
        CheckConstraint("hours > 0 AND hours <= 24", name="hours"),
        UniqueConstraint("timesheet_id", "day", "task_id"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("timesheets.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date]
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="SET NULL")
    )
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class CostRate(IdMixin, Base):
    """An employee's hourly cost from a date, for project labour cost only."""

    __tablename__ = "project_cost_rates"
    __table_args__ = (
        CheckConstraint("hourly >= 0", name="hourly"),
        UniqueConstraint("employee_id", "effective_from"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    hourly: Mapped[Decimal] = mapped_column(Money)
    effective_from: Mapped[date]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProjectSettings(Base):
    """One row (id 1): budget alert thresholds, as percentages of the total budget."""

    __tablename__ = "project_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="one_row"),
        CheckConstraint("warn_at > 0 AND alert_at > warn_at", name="thresholds"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    warn_at: Mapped[int] = mapped_column(SmallInteger, server_default="80")
    alert_at: Mapped[int] = mapped_column(SmallInteger, server_default="100")


class BudgetAlert(IdMixin, Base):
    """A threshold already emailed for a project, so each is sent once."""

    __tablename__ = "project_budget_alerts"
    __table_args__ = (UniqueConstraint("project_id", "threshold"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    threshold: Mapped[int] = mapped_column(SmallInteger)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())
