import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DecisionView:
    step: str
    decision: str
    by: str | None
    at: datetime
    note: str | None


@dataclass(frozen=True, slots=True)
class RequestView:
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    position: str | None
    leave_type_id: uuid.UUID
    type_name: str
    type_code: str
    start_date: date
    end_date: date
    start_half: bool
    end_half: bool
    days: Decimal
    reason: str | None
    status: str
    step: str
    needs_hr: bool
    manager_name: str | None
    certificate_needed: bool
    certificate_file_id: uuid.UUID | None
    created_at: datetime
    decisions: list[DecisionView] = field(default_factory=list)
    can_decide: bool = False
    can_cancel: bool = False

    @property
    def waiting_for_certificate(self) -> bool:
        return self.certificate_needed and self.certificate_file_id is None


@dataclass(frozen=True, slots=True)
class TypeView:
    id: uuid.UUID
    code: str
    name: str
    paid: bool
    counting: str
    has_balance: bool
    accrual: str
    carry_over_max_days: Decimal
    carry_over_expiry_month: int
    requires_hr_approval: bool
    certificate_after_days: Decimal | None
    is_active: bool
    rules: list[tuple[uuid.UUID | None, str, int, Decimal]]  # (grade id, grade, months, days)


@dataclass(frozen=True, slots=True)
class CalendarRow:
    employee_id: uuid.UUID
    name: str
    position: str | None
    # (start day of month, end day of month, type code, type name, status)
    bars: list[tuple[int, int, str, str, str]]
