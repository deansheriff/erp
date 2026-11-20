import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

# Statuses where the person came in.
PRESENT = frozenset({"working", "done", "missing_clock_out"})


@dataclass(frozen=True, slots=True)
class Position:
    """Where the browser says the person is."""

    latitude: Decimal
    longitude: Decimal
    accuracy_m: int | None


@dataclass(frozen=True, slots=True)
class DayView:
    employee_id: uuid.UUID
    day: date
    # off, holiday, on_leave, upcoming, not_in, absent, working, done, missing_clock_out
    status: str
    shift_name: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    late_minutes: int = 0
    early_minutes: int = 0
    worked_minutes: int = 0
    overtime_id: uuid.UUID | None = None
    overtime_minutes: int = 0
    overtime_status: str | None = None
    leave: str | None = None
    leave_part: str | None = None  # full, morning, afternoon
    holiday: str | None = None
    record_id: uuid.UUID | None = None
    photo_taken: bool = False
    photo_file_id: uuid.UUID | None = None
    in_distance_m: int | None = None
    corrected: bool = False
    pending_correction: bool = False

    @property
    def present(self) -> bool:
        return self.status in PRESENT

    @property
    def late(self) -> bool:
        return self.late_minutes > 0

    @property
    def left_early(self) -> bool:
        return self.early_minutes > 0

    @property
    def scheduled(self) -> bool:
        return self.scheduled_start is not None


@dataclass(frozen=True, slots=True)
class PersonDay:
    """A day with the person's name, for team lists."""

    name: str
    position: str | None
    day: DayView


@dataclass(frozen=True, slots=True)
class CorrectionView:
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    work_date: date
    clock_in: datetime
    clock_out: datetime
    reason: str
    status: str
    created_at: datetime
    decided_by: str | None
    decision_note: str | None
    # What the record says now (None when there is none).
    recorded_in: datetime | None
    recorded_out: datetime | None
    can_decide: bool = False
    can_cancel: bool = False


@dataclass(frozen=True, slots=True)
class OvertimeView:
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    work_date: date
    minutes: int
    shift_name: str | None
    scheduled_end: datetime | None
    clock_out: datetime | None
    status: str
    can_decide: bool = False


@dataclass(frozen=True, slots=True)
class SummaryRow:
    employee_id: uuid.UUID
    name: str
    employee_number: str | None
    scheduled: int
    present: int
    absent: int
    late: int
    left_early: int
    missing_clock_out: int
    leave_days: Decimal
    worked_minutes: int
    overtime_approved: int
    overtime_pending: int
