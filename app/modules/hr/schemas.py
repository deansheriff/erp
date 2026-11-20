import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.authz.fields import FieldClass
from app.files.schemas import FileView
from app.modules.hr.fields import HrField


class PersonInput(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    other_names: str | None = Field(default=None, max_length=120)
    work_email: EmailStr | None = None
    user_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    # None keeps the current status (new people start active).
    status: str | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This is required.")
        return value

    @field_validator("other_names")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return " ".join(value.split()) or None if value else None

    @field_validator("status")
    @classmethod
    def _manual_status(cls, value: str | None) -> str | None:
        # "terminated" is set only by an exit (ADR 0017).
        if value is not None and value not in {"active", "on_leave", "suspended"}:
            raise ValueError("Choose one of the options.")
        return value


@dataclass(frozen=True, slots=True)
class PersonRow:
    """Directory fields only (field class `directory`)."""

    id: uuid.UUID
    full_name: str
    first_name: str
    last_name: str
    work_email: str | None
    status: str
    has_account: bool
    positions: list[tuple[str, str]]  # (title, kind), best kind first
    employee_number: str | None = None
    branch: str | None = None

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + self.last_name[:1]).upper()


@dataclass(frozen=True, slots=True)
class ClassAccess:
    read: frozenset[FieldClass]
    update: frozenset[FieldClass]
    is_self: bool

    def can_edit(self, f: HrField) -> bool:
        """Nobody changes their own HR-managed fields (ADR 0017)."""
        return f.field_class in self.update and (not self.is_self or f.self_service)


@dataclass(frozen=True, slots=True)
class FieldValue:
    field: HrField
    value: str  # as submitted in a form (ISO dates, choice keys)
    display: str
    editable: bool


@dataclass(frozen=True, slots=True)
class SectionView:
    key: str
    title: str
    field_class: FieldClass
    fields: list[FieldValue]

    @property
    def editable(self) -> bool:
        return any(f.editable for f in self.fields)

    @property
    def filled(self) -> int:
        return sum(1 for f in self.fields if f.display)


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    id: uuid.UUID
    employee_number: str | None
    full_name: str
    first_name: str
    last_name: str
    other_names: str | None
    work_email: str | None
    status: str
    branch_id: uuid.UUID | None
    branch: str | None
    user_id: uuid.UUID | None
    account_status: str | None
    positions: list[tuple[str, str]]
    manager: str | None
    access: ClassAccess
    sections: list[SectionView]

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + self.last_name[:1]).upper()

    def section(self, key: str) -> SectionView | None:
        return next((s for s in self.sections if s.key == key), None)


@dataclass(frozen=True, slots=True)
class RequestChange:
    label: str
    current: str
    requested: str


@dataclass(frozen=True, slots=True)
class ChangeRequestView:
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_number: str | None
    field_class: str
    status: str
    requested_by: str | None
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    changes: list[RequestChange] = field(default_factory=list)
    can_decide: bool = False

    @property
    def class_label(self) -> str:
        return "Bank details" if self.field_class == "banking" else "Statutory details"


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: uuid.UUID
    title: str
    category: str
    field_class: str
    created_at: datetime
    file: FileView | None
    can_change: bool


@dataclass(frozen=True, slots=True)
class CategoryView:
    id: uuid.UUID
    name: str
    field_class: str
    is_active: bool
    in_use: int


@dataclass(frozen=True, slots=True)
class TemplateTaskView:
    id: uuid.UUID
    title: str
    assignee: str
    due_offset_days: int


@dataclass(frozen=True, slots=True)
class TemplateView:
    id: uuid.UUID
    kind: str
    name: str
    target: str  # "Default", "Department: Finance", "Position: Driver"
    department_id: uuid.UUID | None
    position_id: uuid.UUID | None
    is_active: bool
    tasks: list[TemplateTaskView]


@dataclass(frozen=True, slots=True)
class TaskView:
    id: uuid.UUID
    title: str
    assignee: str
    due_date: date
    done_at: datetime | None
    done_by: str | None
    can_complete: bool
    overdue: bool


@dataclass(frozen=True, slots=True)
class ChecklistView:
    id: uuid.UUID
    kind: str
    name: str
    employee_id: uuid.UUID
    employee_name: str
    position: str | None
    anchor_date: date
    access_ends: datetime | None  # offboarding: when access is revoked
    completed_at: datetime | None
    tasks: list[TaskView]

    @property
    def done(self) -> int:
        return sum(1 for t in self.tasks if t.done_at)

    @property
    def overdue(self) -> int:
        return sum(1 for t in self.tasks if t.overdue)

    @property
    def percent(self) -> int:
        return round(100 * self.done / len(self.tasks)) if self.tasks else 100


@dataclass(frozen=True, slots=True)
class ExitView:
    id: uuid.UUID
    kind: str
    reason: str
    notice_date: date
    last_working_day: date
    final_pay_due: bool
    status: str
    access_ends: datetime
    recorded_by: str | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    id: uuid.UUID
    full_name: str
    initials: str
    position: str | None
    position_kind: str | None
    department: str | None
    branch: str | None
    work_email: str | None


@dataclass(frozen=True, slots=True)
class PayrollFacts:
    name: str
    number: str | None
    hire_date: date | None
    last_day: date | None  # last working day of a scheduled or completed exit
    pfa: str | None
    state: str | None


@dataclass(frozen=True, slots=True)
class RemittanceDetails:
    """Statutory and banking identifiers, decrypted, for payroll's remittance files."""

    tin: str | None
    rsa_pin: str | None
    nhf_number: str | None
    bank_name: str | None
    account_number: str | None
    account_name: str | None
