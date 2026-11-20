"""Staff loans and salary advances (ADR 0022): interest-free, repaid by a fixed monthly
instalment in payroll from a first month until cleared. A Payroll Officer records one and
a Finance Manager approves it; nobody approves a loan they recorded or their own."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz.engine import Resource
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.money import parse_money
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll import ledger
from app.modules.payroll.models import COUNTED, PayrollRun, Payslip, PayslipLine, StaffLoan

READ = "payroll.loan.read"
MANAGE = "payroll.loan.manage"
APPROVE = "payroll.loan.approve"
SEQUENCE = "staff_loan"
KIND_LABELS = {"loan": "Loan", "advance": "Salary advance"}
TABS = ("pending", "repaying", "cleared", "rejected")
MIN_NOTE = 5
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class ScheduleMonth:
    month: date
    amount: Decimal
    repaid: bool  # deducted in an approved, posted or paid run


@dataclass(frozen=True, slots=True)
class LoanView:
    id: uuid.UUID
    number: str
    employee_id: uuid.UUID
    employee_name: str
    kind: str
    amount: Decimal
    instalment: Decimal
    first_month: date
    repaid: Decimal
    balance: Decimal
    status: str  # pending, repaying, cleared, rejected or cancelled
    note: str | None
    recorded_by: str | None
    decided_by: str | None
    decision_note: str | None
    created_at: datetime
    can_decide: bool = False
    can_withdraw: bool = False
    schedule: list[ScheduleMonth] = field(default_factory=list)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def last_month(self) -> date:
        return self.schedule[-1].month if self.schedule else self.first_month


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def instalments(amount: Decimal, instalment: Decimal) -> int:
    whole, rest = divmod(amount, instalment)
    return int(whole) + (1 if rest else 0)


# ------------------------------------------------------------------ recording and deciding


def record(
    db: Session,
    actor: Actor,
    *,
    employee_id: uuid.UUID | None,
    kind: str,
    amount: str,
    instalment: str,
    first_month: date | None,
    note: str = "",
) -> uuid.UUID:
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()
    errors: dict[str, str] = {}
    if employee_id is None or not people.payroll_facts(db, [employee_id]):
        errors["employee_id"] = "Choose who the loan is for."
    if kind not in KIND_LABELS:
        errors["kind"] = "Choose a loan or a salary advance."
    total = principal = None
    try:
        total = parse_money(amount, "amount")
    except ValidationFailed as exc:
        errors.update(exc.field_errors or {})
    try:
        principal = parse_money(instalment, "instalment")
    except ValidationFailed as exc:
        errors.update(exc.field_errors or {})
    if total is not None and total <= 0:
        errors["amount"] = "Enter an amount above zero."
    if principal is not None and principal <= 0:
        errors["instalment"] = "Enter a monthly instalment above zero."
    elif total is not None and principal is not None and principal > total:
        errors["instalment"] = "The instalment can't be more than the amount."
    this_month = clock.today().replace(day=1)
    if first_month is None:
        errors["first_month"] = "Choose the month of the first deduction."
    elif first_month.replace(day=1) < this_month:
        errors["first_month"] = "The first deduction can't be in a past month."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert employee_id and total and principal and first_month  # noqa: S101  (checked above)
    row = StaffLoan(
        number=numbering.next_number(db, SEQUENCE),
        employee_id=employee_id,
        kind=kind,
        amount=total,
        instalment=principal,
        first_month=first_month.replace(day=1),
        note=note.strip() or None,
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.loan.record",
        entity_type="staff_loan",
        entity_id=str(row.id),
        changes=diff_changes(
            {},
            {"number": row.number, "kind": kind, "amount": "set", "instalment": "set"},
            masked_fields=("amount", "instalment"),
        ),
    )
    return row.id


def _borrower_account(db: Session, loan: StaffLoan) -> uuid.UUID | None:
    facts = people.status_and_account(db, loan.employee_id)
    return facts[1] if facts else None


def _may_decide(db: Session, actor: Actor, loan: StaffLoan) -> bool:
    if loan.status != "pending" or not engine.check(db, actor.user_id, APPROVE):
        return False
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        APPROVE,
        actor.user_id,
        {"owner_user_id": _borrower_account(db, loan), "created_by": loan.recorded_by},
        held,
    )


def _locked(db: Session, loan_id: uuid.UUID) -> StaffLoan:
    row = db.scalars(select(StaffLoan).where(StaffLoan.id == loan_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def decide(db: Session, actor: Actor, loan_id: uuid.UUID, *, approve: bool, note: str = "") -> None:
    if not engine.check(db, actor.user_id, APPROVE):
        raise NotFound()
    loan = _locked(db, loan_id)
    if loan.status != "pending":
        raise Conflict("This loan has already been decided.")
    if not _may_decide(db, actor, loan):
        raise PermissionDenied("Someone else approves this loan: not its borrower or recorder.")
    note = note.strip()
    if not approve and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, for the record."})
    loan.status = "approved" if approve else "rejected"
    loan.decided_by, loan.decided_at, loan.decision_note = actor.user_id, clock.now(), note or None
    db.flush()
    if approve:
        name = people.names(db, {loan.employee_id}).get(loan.employee_id, "")
        ledger.queue_loan(
            db, loan, when=clock.today(), memo=f"{KIND_LABELS[loan.kind]} {loan.number}, {name}"
        )
    record_by(
        db,
        actor,
        f"payroll.loan.{'approve' if approve else 'reject'}",
        entity_type="staff_loan",
        entity_id=str(loan.id),
        reason=note or None,
        changes={"status": {"from": "pending", "to": loan.status}},
    )


def withdraw(db: Session, actor: Actor, loan_id: uuid.UUID) -> None:
    """A loan can be withdrawn until it is approved."""
    if not engine.check(db, actor.user_id, MANAGE):
        raise NotFound()
    loan = _locked(db, loan_id)
    if loan.status != "pending":
        raise Conflict("Only a loan waiting for approval can be withdrawn.")
    loan.status = "cancelled"
    db.flush()
    record_by(
        db,
        actor,
        "payroll.loan.withdraw",
        entity_type="staff_loan",
        entity_id=str(loan.id),
        changes={"status": {"from": "pending", "to": "cancelled"}},
    )


# ------------------------------------------------------------------ repayments


def repaid(db: Session, loan_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    """What has been deducted for each loan in approved, posted and paid runs."""
    if not loan_ids:
        return {}
    rows = db.execute(
        select(PayslipLine.loan_id, func.sum(PayslipLine.amount))
        .join(Payslip, Payslip.id == PayslipLine.payslip_id)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(PayslipLine.loan_id.in_(loan_ids), PayrollRun.status.in_(COUNTED))
        .group_by(PayslipLine.loan_id)
    ).all()
    return {loan_id: total for loan_id, total in rows if loan_id}


def due(
    db: Session, employee_ids: list[uuid.UUID], month: date
) -> dict[uuid.UUID, list[tuple[StaffLoan, Decimal]]]:
    """Each person's approved loans with an instalment due in a month: (loan, amount)."""
    if not employee_ids:
        return {}
    loans = list(
        db.scalars(
            select(StaffLoan)
            .where(
                StaffLoan.employee_id.in_(employee_ids),
                StaffLoan.status == "approved",
                StaffLoan.first_month <= month,
            )
            .order_by(StaffLoan.first_month, StaffLoan.number)
        )
    )
    paid = repaid(db, [loan.id for loan in loans])
    result: dict[uuid.UUID, list[tuple[StaffLoan, Decimal]]] = {}
    for loan in loans:
        balance = loan.amount - paid.get(loan.id, ZERO)
        if balance > 0:
            result.setdefault(loan.employee_id, []).append((loan, min(loan.instalment, balance)))
    return result


def _schedule(db: Session, loan: StaffLoan, paid_total: Decimal) -> list[ScheduleMonth]:
    """Months already deducted in counted runs, then the instalments still to come."""
    done = db.execute(
        select(PayrollRun.year, PayrollRun.month, func.sum(PayslipLine.amount))
        .join(Payslip, Payslip.id == PayslipLine.payslip_id)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(PayslipLine.loan_id == loan.id, PayrollRun.status.in_(COUNTED))
        .group_by(PayrollRun.year, PayrollRun.month)
        .order_by(PayrollRun.year, PayrollRun.month)
    ).all()
    months = [ScheduleMonth(date(y, m, 1), amount, repaid=True) for y, m, amount in done]
    if loan.status not in {"approved", "pending"}:
        return months
    balance = loan.amount - paid_total
    upcoming = _next_month(months[-1].month) if months else loan.first_month
    upcoming = max(upcoming, loan.first_month)
    while balance > 0:
        amount = min(loan.instalment, balance)
        months.append(ScheduleMonth(upcoming, amount, repaid=False))
        balance -= amount
        upcoming = _next_month(upcoming)
    return months


# ------------------------------------------------------------------ views


def _status(loan: StaffLoan, balance: Decimal) -> str:
    if loan.status == "approved":
        return "repaying" if balance > 0 else "cleared"
    return loan.status


def _views(
    db: Session, actor: Actor, rows: list[StaffLoan], *, schedules: bool = False
) -> list[LoanView]:
    paid = repaid(db, [r.id for r in rows])
    names = people.names(db, {r.employee_id for r in rows})
    users = {u for r in rows for u in (r.recorded_by, r.decided_by) if u}
    who = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(users))).all())
        if users
        else {}
    )
    can_manage = bool(engine.check(db, actor.user_id, MANAGE))
    views = []
    for r in rows:
        done = paid.get(r.id, ZERO)
        views.append(
            LoanView(
                id=r.id,
                number=r.number,
                employee_id=r.employee_id,
                employee_name=names.get(r.employee_id, ""),
                kind=r.kind,
                amount=r.amount,
                instalment=r.instalment,
                first_month=r.first_month,
                repaid=done,
                balance=r.amount - done if r.status == "approved" else ZERO,
                status=_status(r, r.amount - done),
                note=r.note,
                recorded_by=who.get(r.recorded_by) if r.recorded_by else None,
                decided_by=who.get(r.decided_by) if r.decided_by else None,
                decision_note=r.decision_note,
                created_at=r.created_at,
                can_decide=_may_decide(db, actor, r),
                can_withdraw=can_manage and r.status == "pending",
                schedule=_schedule(db, r, done) if schedules else [],
            )
        )
    return views


def list_loans(db: Session, actor: Actor, tab: str) -> list[LoanView]:
    if not engine.check(db, actor.user_id, READ):
        raise PermissionDenied()
    query = select(StaffLoan).where(engine.scope_filter(db, actor.user_id, READ, StaffLoan))
    if tab == "pending":
        query = query.where(StaffLoan.status == "pending")
    elif tab == "rejected":
        query = query.where(StaffLoan.status.in_(("rejected", "cancelled")))
    else:
        query = query.where(StaffLoan.status == "approved")
    rows = list(db.scalars(query.order_by(StaffLoan.created_at.desc())))
    views = _views(db, actor, rows, schedules=True)
    if tab == "repaying":
        return [v for v in views if v.status == "repaying"]
    if tab == "cleared":
        return [v for v in views if v.status == "cleared"]
    return views


def counts(db: Session, actor: Actor) -> dict[str, int]:
    pending = db.scalar(
        select(func.count())
        .select_from(StaffLoan)
        .where(
            StaffLoan.status == "pending",
            engine.scope_filter(db, actor.user_id, READ, StaffLoan),
        )
    )
    return {"pending": pending or 0}


def mine(db: Session, actor: Actor) -> list[LoanView]:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not engine.check(
        db, actor.user_id, READ, Resource(owner_employee_id=employee_id)
    ):
        return []
    rows = list(
        db.scalars(
            select(StaffLoan)
            .where(StaffLoan.employee_id == employee_id, StaffLoan.status != "cancelled")
            .order_by(StaffLoan.created_at.desc())
        )
    )
    return _views(db, actor, rows, schedules=True)
