import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LineView:
    id: uuid.UUID
    day: date
    category_id: uuid.UUID
    category: str
    description: str
    cost_centre_id: uuid.UUID | None
    cost_centre: str | None
    amount: Decimal
    original_amount: Decimal | None
    receipt_file_id: uuid.UUID | None
    receipt_name: str | None
    no_receipt_reason: str | None
    receipt_needed: bool  # over the category's amount, with no receipt and no reason


@dataclass(frozen=True, slots=True)
class StepView:
    id: uuid.UUID
    position: int
    kind: str
    approver: str | None
    status: str
    decided_by: str | None
    decided_at: datetime | None
    note: str | None


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: uuid.UUID
    number: str
    kind: str
    employee_id: uuid.UUID
    employee_name: str
    position: str | None
    title: str
    total: Decimal
    status: str
    created_at: datetime
    submitted_at: datetime | None
    advance_id: uuid.UUID | None
    advance_number: str | None
    advance_applied: Decimal
    paid_at: date | None
    paid_amount: Decimal | None
    payment_method: str | None
    payment_reference: str | None
    # For an advance: what is left to use or repay.
    outstanding: Decimal | None
    settled: bool
    lines: list[LineView] = field(default_factory=list)
    steps: list[StepView] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # over-limit warnings, plain words
    current_step: str | None = None
    can_edit: bool = False
    can_decide: bool = False
    can_cancel: bool = False

    @property
    def is_advance(self) -> bool:
        return self.kind == "advance"

    @property
    def to_pay(self) -> Decimal:
        return self.paid_amount if self.paid_amount is not None else self.total


@dataclass(frozen=True, slots=True)
class JournalView:
    id: uuid.UUID
    journal_date: date
    memo: str
    status: str
    lines: list[tuple[str, Decimal, Decimal]]  # (account, debit, credit)
