"""Attendance (ADR 0019): shift patterns, who works them (usual shift and roster days),
branch locations, one record per person per work day, corrections and overtime."""

import uuid
from datetime import date, datetime, time
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

DECISION_STATUSES = ("pending", "approved", "rejected")
CORRECTION_STATUSES = (*DECISION_STATUSES, "cancelled")
SOURCES = ("clock", "correction")

Degrees = Numeric(9, 6)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ShiftPattern(IdMixin, Base):
    __tablename__ = "shift_patterns"
    __table_args__ = (
        CheckConstraint("start_time <> end_time", name="times"),
        CheckConstraint("break_minutes BETWEEN 0 AND 600", name="break"),
        CheckConstraint("weekdays ~ '^[0-6]{0,7}$'", name="weekdays"),
        CheckConstraint("late_grace_minutes BETWEEN 0 AND 240", name="late_grace"),
        CheckConstraint("early_grace_minutes BETWEEN 0 AND 240", name="early_grace"),
    )

    name: Mapped[str] = mapped_column(String(80), unique=True)
    start_time: Mapped[time]
    end_time: Mapped[time]  # earlier than start_time: the shift ends the next day
    break_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="60")
    weekdays: Mapped[str] = mapped_column(String(7), server_default="01234")  # Monday = 0
    late_grace_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    early_grace_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ShiftAssignment(IdMixin, Base):
    """A person's usual shift from a date (and until a date, if set)."""

    __tablename__ = "shift_assignments"
    __table_args__ = (CheckConstraint("end_date IS NULL OR start_date <= end_date", name="dates"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    pattern_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shift_patterns.id"))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __authz__: ClassVar[AuthzColumns]


ShiftAssignment.__authz__ = AuthzColumns(owner_employee=ShiftAssignment.employee_id)


class RosterEntry(IdMixin, Base):
    """One day that differs from the usual shift: another pattern, or a day off."""

    __tablename__ = "roster_entries"
    __table_args__ = (UniqueConstraint("employee_id", "day"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    day: Mapped[date]
    pattern_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shift_patterns.id"))
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __authz__: ClassVar[AuthzColumns]


RosterEntry.__authz__ = AuthzColumns(owner_employee=RosterEntry.employee_id)


class AttendanceLocation(IdMixin, Base):
    """Clocking rules for the people of one branch."""

    __tablename__ = "attendance_locations"
    __table_args__ = (
        CheckConstraint(
            "NOT geofence_enabled OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="geofence",
        ),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="latitude"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180", name="longitude"),
        CheckConstraint("radius_m BETWEEN 20 AND 5000", name="radius"),
    )

    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), unique=True
    )
    geofence_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    latitude: Mapped[Decimal | None] = mapped_column(Degrees)
    longitude: Mapped[Decimal | None] = mapped_column(Degrees)
    radius_m: Mapped[int] = mapped_column(Integer, server_default="150")
    require_photo: Mapped[bool] = mapped_column(Boolean, server_default="false")


class AttendanceRecord(IdMixin, Base):
    """A person's work day: first clock-in, last clock-out, and the shift they had (a
    snapshot, so later roster changes don't rewrite history)."""

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date"),
        CheckConstraint("clock_out_at IS NULL OR clock_out_at > clock_in_at", name="times"),
        CheckConstraint(_in("source", SOURCES), name="source"),
        Index("ix_attendance_records_work_date", "work_date"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    work_date: Mapped[date]
    clock_in_at: Mapped[datetime]
    clock_out_at: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(10), server_default="clock")
    # Where they were (metres from the branch when it has a geofence).
    in_latitude: Mapped[Decimal | None] = mapped_column(Degrees)
    in_longitude: Mapped[Decimal | None] = mapped_column(Degrees)
    in_accuracy_m: Mapped[int | None] = mapped_column(Integer)
    in_distance_m: Mapped[int | None] = mapped_column(Integer)
    out_latitude: Mapped[Decimal | None] = mapped_column(Degrees)
    out_longitude: Mapped[Decimal | None] = mapped_column(Degrees)
    out_accuracy_m: Mapped[int | None] = mapped_column(Integer)
    out_distance_m: Mapped[int | None] = mapped_column(Integer)
    # The photo is deleted after its retention period; photo_taken stays true.
    photo_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL")
    )
    photo_taken: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # The shift on that day (None: not scheduled).
    pattern_name: Mapped[str | None] = mapped_column(String(80))
    scheduled_start: Mapped[datetime | None]
    scheduled_end: Mapped[datetime | None]
    break_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    late_grace_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    early_grace_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    corrected_at: Mapped[datetime | None]

    __authz__: ClassVar[AuthzColumns]


AttendanceRecord.__authz__ = AuthzColumns(owner_employee=AttendanceRecord.employee_id)


class AttendanceCorrection(IdMixin, Base):
    __tablename__ = "attendance_corrections"
    __table_args__ = (
        CheckConstraint(_in("status", CORRECTION_STATUSES), name="status"),
        CheckConstraint("clock_out_at > clock_in_at", name="times"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    work_date: Mapped[date]
    clock_in_at: Mapped[datetime]
    clock_out_at: Mapped[datetime]
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


AttendanceCorrection.__authz__ = AuthzColumns(owner_employee=AttendanceCorrection.employee_id)

# One open correction per person and day.
Index(
    "uq_attendance_correction_pending",
    AttendanceCorrection.employee_id,
    AttendanceCorrection.work_date,
    unique=True,
    postgresql_where=AttendanceCorrection.status == "pending",
)


class OvertimeEntry(IdMixin, Base):
    """Time past the scheduled end of a shift. Counts for payroll only once approved."""

    __tablename__ = "overtime_entries"
    __table_args__ = (
        CheckConstraint(_in("status", DECISION_STATUSES), name="status"),
        CheckConstraint("minutes > 0", name="minutes"),
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attendance_records.id", ondelete="CASCADE"), unique=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    work_date: Mapped[date]
    minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)

    __authz__: ClassVar[AuthzColumns]


OvertimeEntry.__authz__ = AuthzColumns(owner_employee=OvertimeEntry.employee_id)
