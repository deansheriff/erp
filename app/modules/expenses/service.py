"""Expense claims and cash advances (ADR 0020): drafting, lines and receipts, submission
and the approval chain, deciding, payment, advance settlement and repayments, and the
journals queued for the ledger."""

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz import service as authz
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.formatting import format_money
from app.core.numbering import next_number
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.expenses import settings as config
from app.modules.expenses.files import EXPENSE_RECEIPT
from app.modules.expenses.models import (
    AdvanceRepayment,
    ExpenseCategory,
    ExpenseDocument,
    ExpenseJournal,
    ExpenseJournalLine,
    ExpenseLine,
    ExpenseStep,
)
from app.modules.expenses.schemas import DocumentView, JournalView, LineView, StepView
from app.modules.hr import service as people
from app.org import service as org

CREATE = "expenses.claim.create"
READ = "expenses.claim.read"
APPROVE = "expenses.claim.approve"
APPROVE_ANY = "expenses.claim.approve_any"
APPROVE_FINANCE = "expenses.claim.approve_finance"
REIMBURSE = "expenses.claim.reimburse"
OVERRIDE = "expenses.claim.reimburse_override"
EDITABLE = ("draft", "returned")
MIN_NOTE = 5
ZERO = Decimal("0.00")
SEQUENCES = {"claim": "expense_claim", "advance": "expense_advance"}


def _can(db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None) -> bool:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    return bool(engine.check(db, actor.user_id, permission, resource))


def _naira(amount: Decimal) -> str:
    return format_money(amount, "NGN")


def _document(db: Session, document_id: uuid.UUID, *, lock: bool = False) -> ExpenseDocument:
    stmt = select(ExpenseDocument).where(ExpenseDocument.id == document_id)
    row = db.scalars(stmt.with_for_update() if lock else stmt).one_or_none()
    if row is None:
        raise NotFound()
    return row


def _visible(db: Session, actor: Actor, doc: ExpenseDocument) -> bool:
    return _can(db, actor, READ, doc.employee_id) or _decider_for(db, actor, doc) is not None


# ------------------------------------------------------------------ drafting


def _me(db: Session, actor: Actor) -> uuid.UUID:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not _can(db, actor, CREATE, employee_id):
        raise PermissionDenied()
    facts = people.status_and_account(db, employee_id)
    if facts is None or facts[0] == "terminated":
        raise Conflict("This person has left the organisation.")
    return employee_id


def _title(title: str, field: str = "title") -> str:
    title = " ".join(title.split())
    if not title:
        raise ValidationFailed(field_errors={field: "Say what this is for."})
    return title[:200]


def open_advances(db: Session, employee_id: uuid.UUID) -> list[ExpenseDocument]:
    """Paid advances with something left, that a claim can be settled against."""
    rows = db.scalars(
        select(ExpenseDocument).where(
            ExpenseDocument.employee_id == employee_id,
            ExpenseDocument.kind == "advance",
            ExpenseDocument.status == "reimbursed",
            ExpenseDocument.settled_at.is_(None),
        )
    )
    return [r for r in rows if outstanding(db, r) > 0]


def _check_advance(db: Session, employee_id: uuid.UUID, advance_id: uuid.UUID | None) -> None:
    if advance_id is not None and advance_id not in {a.id for a in open_advances(db, employee_id)}:
        raise ValidationFailed(field_errors={"advance_id": "Choose one of your paid advances."})


def create(
    db: Session,
    actor: Actor,
    *,
    kind: str,
    title: str,
    amount: str = "",
    advance_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A new draft claim, or a draft advance request with its amount."""
    employee_id = _me(db, actor)
    if kind not in SEQUENCES:
        raise NotFound()
    title = _title(title)
    total = ZERO
    if kind == "advance":
        total = config.parse_money(amount, "amount") or ZERO
        if total <= 0:
            raise ValidationFailed(field_errors={"amount": "Enter an amount above zero."})
        advance_id = None
    else:
        _check_advance(db, employee_id, advance_id)
    doc = ExpenseDocument(
        number=next_number(db, SEQUENCES[kind]),
        kind=kind,
        employee_id=employee_id,
        title=title,
        total=total,
        advance_id=advance_id,
        created_by=actor.user_id,
    )
    db.add(doc)
    db.flush()
    record_by(
        db,
        actor,
        f"expenses.{kind}.create",
        entity_type="expense_document",
        entity_id=str(doc.id),
        changes=diff_changes({}, {"number": doc.number, "title": title, "total": str(total)}),
    )
    return doc.id


def _editable(db: Session, actor: Actor, document_id: uuid.UUID) -> ExpenseDocument:
    doc = _document(db, document_id, lock=True)
    if not _can(db, actor, READ, doc.employee_id):
        raise NotFound()
    if (
        not _can(db, actor, CREATE, doc.employee_id)
        or people.employee_of_user(db, actor.user_id) != doc.employee_id
    ):
        raise PermissionDenied()
    if doc.status not in EDITABLE:
        raise Conflict("This has been sent for approval and can't be changed now.")
    return doc


def update_header(
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    title: str,
    amount: str = "",
    advance_id: uuid.UUID | None = None,
) -> None:
    doc = _editable(db, actor, document_id)
    before = {"title": doc.title, "total": str(doc.total), "advance": str(doc.advance_id)}
    doc.title = _title(title)
    if doc.kind == "advance":
        total = config.parse_money(amount, "amount") or ZERO
        if total <= 0:
            raise ValidationFailed(field_errors={"amount": "Enter an amount above zero."})
        doc.total = total
    else:
        _check_advance(db, doc.employee_id, advance_id)
        doc.advance_id = advance_id
    record_by(
        db,
        actor,
        "expenses.document.update",
        entity_type="expense_document",
        entity_id=str(doc.id),
        changes=diff_changes(
            before, {"title": doc.title, "total": str(doc.total), "advance": str(doc.advance_id)}
        ),
    )


def _recount(db: Session, doc: ExpenseDocument) -> None:
    if doc.kind == "claim":
        db.flush()
        doc.total = (
            db.scalar(
                select(func.coalesce(func.sum(ExpenseLine.amount), 0)).where(
                    ExpenseLine.document_id == doc.id
                )
            )
            or ZERO
        )


def save_line(
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    line_id: uuid.UUID | None,
    *,
    day: date | None,
    category_id: uuid.UUID | None,
    description: str,
    cost_centre_id: uuid.UUID | None,
    amount: str,
    no_receipt_reason: str = "",
) -> uuid.UUID:
    doc = _editable(db, actor, document_id)
    if doc.kind != "claim":
        raise NotFound()
    errors = {}
    if day is None:
        errors["day"] = "Choose the date."
    elif day > clock.today():
        errors["day"] = "The date can't be in the future."
    category = db.get(ExpenseCategory, category_id) if category_id else None
    if category is None or not category.is_active:
        errors["category_id"] = "Choose a category."
    if not description.strip():
        errors["description"] = "Say what it was."
    if cost_centre_id is not None and cost_centre_id not in dict(org.cost_centre_choices(db)):
        errors["cost_centre_id"] = "Choose a cost centre."
    if errors:
        raise ValidationFailed(field_errors=errors)
    value = config.parse_money(amount, "amount") or ZERO
    if value <= 0:
        raise ValidationFailed(field_errors={"amount": "Enter an amount above zero."})
    assert day is not None and category is not None  # noqa: S101
    if line_id is None:
        line = ExpenseLine(
            document_id=doc.id,
            sort_order=(
                db.scalar(
                    select(func.max(ExpenseLine.sort_order)).where(
                        ExpenseLine.document_id == doc.id
                    )
                )
                or 0
            )
            + 1,
            day=day,
            category_id=category.id,
            description=description.strip()[:200],
            cost_centre_id=cost_centre_id,
            amount=value,
        )
        db.add(line)
    else:
        found = db.get(ExpenseLine, line_id)
        if found is None or found.document_id != doc.id:
            raise NotFound()
        line = found
        line.day, line.category_id, line.cost_centre_id = day, category.id, cost_centre_id
        line.description, line.amount = description.strip()[:200], value
    line.no_receipt_reason = no_receipt_reason.strip()[:200] or None
    _recount(db, doc)
    db.flush()
    record_by(
        db,
        actor,
        "expenses.line.save",
        entity_type="expense_document",
        entity_id=str(doc.id),
        changes={"line": {"from": None, "to": f"{day.isoformat()} {category.name} {value}"}},
    )
    return line.id


def delete_line(db: Session, actor: Actor, line_id: uuid.UUID) -> uuid.UUID:
    line = db.get(ExpenseLine, line_id)
    if line is None:
        raise NotFound()
    doc = _editable(db, actor, line.document_id)
    db.delete(line)
    _recount(db, doc)
    record_by(
        db, actor, "expenses.line.delete", entity_type="expense_document", entity_id=str(doc.id)
    )
    return doc.id


def attach_receipt(
    db: Session,
    actor: Actor,
    line_id: uuid.UUID,
    *,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
) -> uuid.UUID:
    line = db.get(ExpenseLine, line_id)
    if line is None:
        raise NotFound()
    doc = _editable(db, actor, line.document_id)
    line.receipt_file_id = files.upload(
        db,
        actor,
        purpose_key=EXPENSE_RECEIPT.key,
        entity_id=str(line.id),
        filename=filename,
        declared_mime=declared_mime,
        data=data,
        replaces=line.receipt_file_id,
    )
    line.no_receipt_reason = None
    return doc.id


# ------------------------------------------------------------------ submitting


def _receipt_needed(line: ExpenseLine, category: ExpenseCategory) -> bool:
    return (
        line.amount > category.receipt_above
        and line.receipt_file_id is None
        and not line.no_receipt_reason
    )


def _build_steps(db: Session, doc: ExpenseDocument) -> None:
    settings = config.get_settings(db)
    submission = (
        db.scalar(select(func.max(ExpenseStep.submission)).where(ExpenseStep.document_id == doc.id))
        or 0
    ) + 1
    chain = org.approval_chain(db, doc.employee_id, 2)
    plan: list[tuple[str, uuid.UUID | None]] = [("manager", chain[0] if chain else None)]
    if doc.total > settings.second_level_above and len(chain) > 1:
        plan.append(("second", chain[1]))
    if doc.total > settings.finance_above:
        plan.append(("finance", None))
    for position, (kind, approver) in enumerate(plan, start=1):
        db.add(
            ExpenseStep(
                document_id=doc.id,
                submission=submission,
                position=position,
                kind=kind,
                approver_employee_id=approver,
                status="pending" if position == 1 else "waiting",
            )
        )


def submit(db: Session, actor: Actor, document_id: uuid.UUID) -> None:
    doc = _editable(db, actor, document_id)
    if doc.kind == "claim":
        lines = list(db.scalars(select(ExpenseLine).where(ExpenseLine.document_id == doc.id)))
        if not lines:
            raise ValidationFailed(field_errors={"form": "Add at least one line first."})
        categories = {c.id: c for c in db.scalars(select(ExpenseCategory))}
        missing = [line for line in lines if _receipt_needed(line, categories[line.category_id])]
        if missing:
            raise ValidationFailed(
                field_errors={
                    "form": f"{len(missing)} line{'s need' if len(missing) > 1 else ' needs'} "
                    "a receipt, or a reason why there isn't one."
                }
            )
        _check_advance(db, doc.employee_id, doc.advance_id)
        _recount(db, doc)
    before = doc.status
    doc.status, doc.submitted_at, doc.decided_at = "submitted", clock.now(), None
    _build_steps(db, doc)
    db.flush()
    record_by(
        db,
        actor,
        f"expenses.{doc.kind}.submit",
        entity_type="expense_document",
        entity_id=str(doc.id),
        changes={
            "status": {"from": before, "to": "submitted"},
            "total": {"from": None, "to": str(doc.total)},
        },
    )


def cancel(db: Session, actor: Actor, document_id: uuid.UUID) -> None:
    doc = _document(db, document_id, lock=True)
    if not _can(db, actor, READ, doc.employee_id):
        raise NotFound()
    if not _can_cancel(db, actor, doc):
        raise Conflict("Only drafts and documents waiting for approval can be cancelled.")
    before = doc.status
    doc.status = "cancelled"
    for step in db.scalars(
        select(ExpenseStep).where(
            ExpenseStep.document_id == doc.id, ExpenseStep.status.in_(("pending", "waiting"))
        )
    ):
        db.delete(step)
    record_by(
        db,
        actor,
        f"expenses.{doc.kind}.cancel",
        entity_type="expense_document",
        entity_id=str(doc.id),
        changes={"status": {"from": before, "to": "cancelled"}},
    )


def _can_cancel(db: Session, actor: Actor, doc: ExpenseDocument) -> bool:
    mine = people.employee_of_user(db, actor.user_id) == doc.employee_id
    return mine and doc.status in {"draft", "submitted", "returned"}


# ------------------------------------------------------------------ deciding


def _pending_step(db: Session, doc: ExpenseDocument) -> ExpenseStep | None:
    return db.scalars(
        select(ExpenseStep).where(
            ExpenseStep.document_id == doc.id, ExpenseStep.status == "pending"
        )
    ).first()


def _owner_user(db: Session, doc: ExpenseDocument) -> uuid.UUID | None:
    facts = people.status_and_account(db, doc.employee_id)
    return facts[1] if facts else None


def _sod_clear(db: Session, actor: Actor, permission: str, doc: ExpenseDocument) -> bool:
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        permission,
        actor.user_id,
        {"owner_user_id": _owner_user(db, doc)},
        held,
    )


def _step_permission(db: Session, actor: Actor, step: ExpenseStep) -> str | None:
    """The permission the actor would decide this step with, if any."""
    if step.kind == "finance":
        return APPROVE_FINANCE if _can(db, actor, APPROVE_FINANCE) else None
    if step.approver_employee_id is not None and _can(db, actor, APPROVE):
        approver_user = people.accounts_of(db, {step.approver_employee_id}).get(
            step.approver_employee_id
        )
        acting_for = authz.delegators(db, actor.user_id, APPROVE)
        if approver_user is not None and (
            approver_user == actor.user_id or approver_user in acting_for
        ):
            return APPROVE
    return APPROVE_ANY if _can(db, actor, APPROVE_ANY) else None


def _decider_for(db: Session, actor: Actor, doc: ExpenseDocument) -> str | None:
    """The permission the actor may decide the current step with, or None."""
    if doc.status != "submitted":
        return None
    step = _pending_step(db, doc)
    if step is None:
        return None
    permission = _step_permission(db, actor, step)
    if permission is None or not _sod_clear(db, actor, permission, doc):
        return None
    return permission


def _apply_corrections(
    db: Session, doc: ExpenseDocument, corrections: dict[uuid.UUID, str]
) -> dict[str, dict[str, object]]:
    """Approvers' changes to line amounts; the employee's amount is kept on the line."""
    changes: dict[str, dict[str, object]] = {}
    if not corrections or doc.kind != "claim":
        return changes
    lines = {
        line.id: line
        for line in db.scalars(select(ExpenseLine).where(ExpenseLine.document_id == doc.id))
    }
    for line_id, text in corrections.items():
        line = lines.get(line_id)
        if line is None or not text.strip():
            continue
        value = config.parse_money(text, f"amount_{line_id}") or ZERO
        if value <= 0:
            raise ValidationFailed(
                field_errors={f"amount_{line_id}": "Enter an amount above zero."}
            )
        if value != line.amount:
            changes[f"line {line.description}"] = {"from": str(line.amount), "to": str(value)}
            if line.original_amount is None:
                line.original_amount = line.amount
            line.amount = value
    _recount(db, doc)
    return changes


def _move_on(db: Session, doc: ExpenseDocument, status: str) -> None:
    """After a step: the next step waits for its approver, or the document is decided."""
    waiting = list(
        db.scalars(
            select(ExpenseStep)
            .where(ExpenseStep.document_id == doc.id, ExpenseStep.status == "waiting")
            .order_by(ExpenseStep.position)
        )
    )
    if status == "approved" and waiting:
        waiting[0].status = "pending"
        return
    doc.status, doc.decided_at = status, clock.now()
    for later in waiting:
        db.delete(later)


def decide(
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    decision: str,
    note: str = "",
    corrections: dict[uuid.UUID, str] | None = None,
) -> None:
    """Approve, reject or return the current step. Approvers may correct line amounts
    first; the employee's amount is kept and the change audited."""
    if decision not in {"approve", "reject", "return"}:
        raise NotFound()
    doc = _document(db, document_id, lock=True)
    if not _visible(db, actor, doc):
        raise NotFound()
    if doc.status != "submitted":
        raise Conflict("This has already been decided.")
    step = _pending_step(db, doc)
    assert step is not None  # noqa: S101
    if _decider_for(db, actor, doc) is None:
        permission = _step_permission(db, actor, step)
        if permission is not None:
            raise PermissionDenied("You can't approve your own claim.")
        raise PermissionDenied("This step is waiting for someone else.")
    note = note.strip()
    if decision != "approve" and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, so they know what to change."})
    changes = _apply_corrections(db, doc, corrections or {})
    status = {"approve": "approved", "reject": "rejected", "return": "returned"}[decision]
    step.status, step.decided_by, step.decided_at = status, actor.user_id, clock.now()
    step.note = note or None
    before = doc.status
    _move_on(db, doc, status)
    changes["step"] = {"from": None, "to": f"{step.kind} {status}"}
    changes["status"] = {"from": before, "to": doc.status}
    record_by(
        db,
        actor,
        f"expenses.{doc.kind}.{decision}",
        entity_type="expense_document",
        entity_id=str(doc.id),
        reason=note or None,
        changes=changes,
    )


# ------------------------------------------------------------------ paying


def outstanding(db: Session, advance: ExpenseDocument) -> Decimal:
    """What is left of a paid advance: not yet used by claims nor repaid."""
    if advance.kind != "advance" or advance.status != "reimbursed":
        return ZERO
    used = (
        db.scalar(
            select(func.coalesce(func.sum(ExpenseDocument.advance_applied), 0)).where(
                ExpenseDocument.advance_id == advance.id, ExpenseDocument.status == "reimbursed"
            )
        )
        or ZERO
    )
    repaid = (
        db.scalar(
            select(func.coalesce(func.sum(AdvanceRepayment.amount), 0)).where(
                AdvanceRepayment.advance_id == advance.id
            )
        )
        or ZERO
    )
    return (advance.paid_amount or ZERO) - used - repaid


def _approved_by(db: Session, doc: ExpenseDocument) -> set[uuid.UUID]:
    return set(
        db.scalars(
            select(ExpenseStep.decided_by).where(
                ExpenseStep.document_id == doc.id,
                ExpenseStep.status == "approved",
                ExpenseStep.decided_by.is_not(None),
            )
        )
    )


def can_pay(db: Session, actor: Actor, doc: ExpenseDocument) -> bool:
    if doc.status != "approved" or not _can(db, actor, REIMBURSE):
        return False
    if not _sod_clear(db, actor, REIMBURSE, doc):
        return False
    return actor.user_id not in _approved_by(db, doc) or _can(db, actor, OVERRIDE)


def _journal(
    db: Session,
    doc: ExpenseDocument,
    *,
    when: date,
    memo: str,
    lines: list[tuple[str, uuid.UUID | None, Decimal, Decimal]],
    repayment_id: uuid.UUID | None = None,
) -> None:
    debit = sum((d for _, _, d, _ in lines), ZERO)
    credit = sum((c for _, _, _, c in lines), ZERO)
    assert debit == credit, "unbalanced journal"  # noqa: S101
    journal = ExpenseJournal(
        document_id=doc.id, repayment_id=repayment_id, journal_date=when, memo=memo[:200]
    )
    db.add(journal)
    db.flush()
    for account, cost_centre, d, c in lines:
        if d or c:
            db.add(
                ExpenseJournalLine(
                    journal_id=journal.id,
                    account=account,
                    cost_centre_id=cost_centre,
                    debit=d,
                    credit=c,
                )
            )


def _payment_lines(
    db: Session, doc: ExpenseDocument, applied: Decimal, paid: Decimal
) -> list[tuple[str, uuid.UUID | None, Decimal, Decimal]]:
    settings = config.get_settings(db)
    if doc.kind == "advance":
        return [
            (settings.advances_account, None, paid, ZERO),
            (settings.bank_account, None, ZERO, paid),
        ]
    categories = {c.id: c for c in db.scalars(select(ExpenseCategory))}
    by_account: dict[tuple[str, uuid.UUID | None], Decimal] = defaultdict(lambda: ZERO)
    for line in db.scalars(select(ExpenseLine).where(ExpenseLine.document_id == doc.id)):
        by_account[(categories[line.category_id].gl_account, line.cost_centre_id)] += line.amount
    result = [(account, cc, amount, ZERO) for (account, cc), amount in by_account.items()]
    result.append((settings.advances_account, None, ZERO, applied))
    result.append((settings.bank_account, None, ZERO, paid))
    return result


def _check_payment(
    document_ids: list[uuid.UUID], paid_on: date | None, method: str, reference: str
) -> date:
    errors = {}
    if not document_ids:
        errors["form"] = "Choose what to pay."
    if paid_on is None:
        errors["paid_on"] = "Choose the payment date."
    elif paid_on > clock.today():
        errors["paid_on"] = "The payment date can't be in the future."
    if method not in {"transfer", "cash"}:
        errors["method"] = "Choose how it was paid."
    if method == "transfer" and not reference.strip():
        errors["reference"] = "Enter the transfer reference."
    if errors or paid_on is None:
        raise ValidationFailed(field_errors=errors)
    return paid_on


def pay(
    db: Session,
    actor: Actor,
    document_ids: list[uuid.UUID],
    *,
    paid_on: date | None,
    method: str,
    reference: str,
) -> int:
    """Mark approved claims and advances paid. A claim uses what is left of its advance
    first and is paid the rest. Each payment queues a journal for the ledger."""
    if not _can(db, actor, REIMBURSE):
        raise PermissionDenied()
    paid_on = _check_payment(document_ids, paid_on, method, reference)
    done = 0
    for document_id in sorted(set(document_ids)):
        doc = _document(db, document_id, lock=True)
        if doc.status != "approved":
            raise Conflict(f"{doc.number} isn't waiting for payment.")
        if not can_pay(db, actor, doc):
            if not _sod_clear(db, actor, REIMBURSE, doc):
                raise PermissionDenied(f"You can't pay your own claim ({doc.number}).")
            raise PermissionDenied(f"You approved {doc.number}, so someone else must pay it.")
        applied = ZERO
        if doc.advance_id is not None:
            advance = _document(db, doc.advance_id, lock=True)
            applied = min(doc.total, max(outstanding(db, advance), ZERO))
        paid = doc.total - applied
        doc.status, doc.advance_applied, doc.paid_amount = "reimbursed", applied, paid
        doc.paid_at, doc.payment_method = paid_on, method
        doc.payment_reference, doc.paid_by = reference.strip()[:80] or None, actor.user_id
        db.flush()
        _journal(
            db,
            doc,
            when=paid_on,
            memo=f"{doc.number} {doc.title}",
            lines=_payment_lines(db, doc, applied, paid),
        )
        if doc.advance_id is not None:
            advance = _document(db, doc.advance_id)
            if outstanding(db, advance) <= 0:
                advance.settled_at = clock.now()
        record_by(
            db,
            actor,
            f"expenses.{doc.kind}.pay",
            entity_type="expense_document",
            entity_id=str(doc.id),
            changes={
                "status": {"from": "approved", "to": "reimbursed"},
                "paid": {"from": None, "to": str(paid)},
                "advance_applied": {"from": None, "to": str(applied)},
            },
        )
        done += 1
    return done


def _check_repayment(
    db: Session, advance: ExpenseDocument, amount: str, received_on: date | None, method: str
) -> Decimal:
    left = outstanding(db, advance)
    value = config.parse_money(amount, "repay_amount") or ZERO
    errors = {}
    if value <= 0:
        errors["repay_amount"] = "Enter an amount above zero."
    elif value > left:
        errors["repay_amount"] = f"Only {_naira(left)} is owed."
    if received_on is None:
        errors["received_on"] = "Choose the date it was received."
    elif received_on > clock.today():
        errors["received_on"] = "The date can't be in the future."
    if method not in {"transfer", "cash"}:
        errors["repay_method"] = "Choose how it was paid."
    if errors:
        raise ValidationFailed(field_errors=errors)
    return value


def record_repayment(
    db: Session,
    actor: Actor,
    advance_id: uuid.UUID,
    *,
    amount: str,
    received_on: date | None,
    method: str,
    reference: str,
) -> None:
    if not _can(db, actor, REIMBURSE):
        raise PermissionDenied()
    advance = _document(db, advance_id, lock=True)
    if advance.kind != "advance" or advance.status != "reimbursed" or advance.settled_at:
        raise Conflict("Nothing is owed on this advance.")
    value = _check_repayment(db, advance, amount, received_on, method)
    assert received_on is not None  # noqa: S101
    row = AdvanceRepayment(
        advance_id=advance.id,
        amount=value,
        received_on=received_on,
        method=method,
        reference=reference.strip()[:80] or None,
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    settings = config.get_settings(db)
    _journal(
        db,
        advance,
        when=received_on,
        memo=f"{advance.number} repayment",
        lines=[
            (settings.bank_account, None, value, ZERO),
            (settings.advances_account, None, ZERO, value),
        ],
        repayment_id=row.id,
    )
    if outstanding(db, advance) <= 0:
        advance.settled_at = clock.now()
    record_by(
        db,
        actor,
        "expenses.advance.repayment",
        entity_type="expense_document",
        entity_id=str(advance.id),
        changes={"repaid": {"from": None, "to": str(value)}},
    )


# ------------------------------------------------------------------ limits


def _flags(db: Session, doc: ExpenseDocument, lines: list[ExpenseLine]) -> list[str]:
    """Over-limit warnings for approvers (claims can still be submitted)."""
    if doc.kind != "claim" or not lines:
        return []
    flags = []
    categories = {c.id: c for c in db.scalars(select(ExpenseCategory))}
    per_category: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for line in lines:
        per_category[line.category_id] += line.amount
    for category_id, amount in per_category.items():
        limit = categories[category_id].claim_limit
        if limit is not None and amount > limit:
            flags.append(
                f"{categories[category_id].name} is {_naira(amount)}, over its "
                f"{_naira(limit)} limit per claim."
            )
    weekly, monthly = config.limits_for(db, doc.employee_id)
    counted = select(ExpenseDocument.id).where(
        ExpenseDocument.employee_id == doc.employee_id,
        ExpenseDocument.kind == "claim",
        or_(
            ExpenseDocument.id == doc.id,
            ExpenseDocument.status.in_(("submitted", "approved", "reimbursed")),
        ),
    )

    def spent(start: date, end: date) -> Decimal:
        return (
            db.scalar(
                select(func.coalesce(func.sum(ExpenseLine.amount), 0)).where(
                    ExpenseLine.document_id.in_(counted),
                    ExpenseLine.day >= start,
                    ExpenseLine.day <= end,
                )
            )
            or ZERO
        )

    if weekly is not None:
        for monday in sorted({line.day - timedelta(days=line.day.weekday()) for line in lines}):
            total = spent(monday, monday + timedelta(days=6))
            if total > weekly:
                flags.append(
                    f"Spending in the week of {monday:%d %b} is {_naira(total)}, over the "
                    f"{_naira(weekly)} weekly limit."
                )
    if monthly is not None:
        for first in sorted({line.day.replace(day=1) for line in lines}):
            last = date(first.year + first.month // 12, first.month % 12 + 1, 1) - timedelta(days=1)
            total = spent(first, last)
            if total > monthly:
                flags.append(
                    f"Spending in {first:%B %Y} is {_naira(total)}, over the "
                    f"{_naira(monthly)} monthly limit."
                )
    return flags


# ------------------------------------------------------------------ views


def _views(
    db: Session, actor: Actor, rows: list[ExpenseDocument], *, full: bool = False
) -> list[DocumentView]:
    if not rows:
        return []
    ids = [r.id for r in rows]
    names = people.names(db, {r.employee_id for r in rows})
    held = org.positions_held(db, list({r.employee_id for r in rows}))
    advances = {
        a.id: a.number
        for a in db.scalars(
            select(ExpenseDocument).where(
                ExpenseDocument.id.in_({r.advance_id for r in rows if r.advance_id})
            )
        )
    }
    current: dict[uuid.UUID, str] = {
        s.document_id: s.kind
        for s in db.scalars(
            select(ExpenseStep).where(
                ExpenseStep.document_id.in_(ids), ExpenseStep.status == "pending"
            )
        )
    }
    lines: dict[uuid.UUID, list[ExpenseLine]] = defaultdict(list)
    steps: dict[uuid.UUID, list[ExpenseStep]] = defaultdict(list)
    if full:
        for line in db.scalars(
            select(ExpenseLine)
            .where(ExpenseLine.document_id.in_(ids))
            .order_by(ExpenseLine.sort_order)
        ):
            lines[line.document_id].append(line)
        for step in db.scalars(
            select(ExpenseStep)
            .where(ExpenseStep.document_id.in_(ids))
            .order_by(ExpenseStep.submission, ExpenseStep.position)
        ):
            steps[step.document_id].append(step)
    categories = {c.id: c for c in db.scalars(select(ExpenseCategory))} if full else {}
    centres = dict(org.cost_centre_choices(db)) if full else {}
    receipts = (
        files.latest_versions(
            db,
            [
                g
                for g in (
                    files.group_of(db, line.receipt_file_id)
                    for ls in lines.values()
                    for line in ls
                    if line.receipt_file_id
                )
                if g
            ],
        )
        if full
        else {}
    )
    receipt_names = {v.id: v.original_filename for v in receipts.values()}
    approvers = people.names(
        db, {s.approver_employee_id for ss in steps.values() for s in ss if s.approver_employee_id}
    )
    deciders = dict(
        db.execute(
            select(User.id, User.full_name).where(
                User.id.in_({s.decided_by for ss in steps.values() for s in ss if s.decided_by})
            )
        ).all()
    )
    result = []
    for r in rows:
        own_lines = lines[r.id]
        result.append(
            DocumentView(
                id=r.id,
                number=r.number,
                kind=r.kind,
                employee_id=r.employee_id,
                employee_name=names.get(r.employee_id, ""),
                position=held[r.employee_id][0][0] if held.get(r.employee_id) else None,
                title=r.title,
                total=r.total,
                status=r.status,
                created_at=r.created_at,
                submitted_at=r.submitted_at,
                advance_id=r.advance_id,
                advance_number=advances.get(r.advance_id) if r.advance_id else None,
                advance_applied=r.advance_applied,
                paid_at=r.paid_at,
                paid_amount=r.paid_amount,
                payment_method=r.payment_method,
                payment_reference=r.payment_reference,
                outstanding=outstanding(db, r) if r.kind == "advance" else None,
                settled=r.settled_at is not None,
                lines=[
                    LineView(
                        id=line.id,
                        day=line.day,
                        category_id=line.category_id,
                        category=categories[line.category_id].name,
                        description=line.description,
                        cost_centre_id=line.cost_centre_id,
                        cost_centre=centres.get(line.cost_centre_id)
                        if line.cost_centre_id
                        else None,
                        amount=line.amount,
                        original_amount=line.original_amount,
                        receipt_file_id=line.receipt_file_id,
                        receipt_name=receipt_names.get(line.receipt_file_id)
                        if line.receipt_file_id
                        else None,
                        no_receipt_reason=line.no_receipt_reason,
                        receipt_needed=_receipt_needed(line, categories[line.category_id]),
                    )
                    for line in own_lines
                ],
                steps=[
                    StepView(
                        id=s.id,
                        position=s.position,
                        kind=s.kind,
                        approver=approvers.get(s.approver_employee_id)
                        if s.approver_employee_id
                        else None,
                        status=s.status,
                        decided_by=deciders.get(s.decided_by) if s.decided_by else None,
                        decided_at=s.decided_at,
                        note=s.note,
                    )
                    for s in steps[r.id]
                    if s.submission == max(x.submission for x in steps[r.id])
                ],
                flags=_flags(db, r, own_lines) if full else [],
                current_step=current.get(r.id),
                can_edit=r.status in EDITABLE
                and people.employee_of_user(db, actor.user_id) == r.employee_id,
                can_decide=_decider_for(db, actor, r) is not None,
                can_cancel=_can_cancel(db, actor, r),
            )
        )
    return result


def get(db: Session, actor: Actor, document_id: uuid.UUID) -> DocumentView:
    doc = _document(db, document_id)
    if not _visible(db, actor, doc):
        raise NotFound()
    return _views(db, actor, [doc], full=True)[0]


def mine(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[DocumentView]:
    rows = list(
        db.scalars(
            select(ExpenseDocument)
            .where(ExpenseDocument.employee_id == employee_id)
            .order_by(ExpenseDocument.created_at.desc())
            .limit(100)
        )
    )
    return _views(db, actor, rows)


def inbox(db: Session, actor: Actor) -> list[DocumentView]:
    """Documents whose current step the actor may decide now."""
    kinds = []
    if _can(db, actor, APPROVE_FINANCE):
        kinds.append("finance")
    stmt = (
        select(ExpenseDocument)
        .join(ExpenseStep, ExpenseStep.document_id == ExpenseDocument.id)
        .where(ExpenseDocument.status == "submitted", ExpenseStep.status == "pending")
    )
    clauses = []
    if kinds:
        clauses.append(ExpenseStep.kind.in_(kinds))
    if _can(db, actor, APPROVE_ANY):
        clauses.append(ExpenseStep.kind.in_(("manager", "second")))
    elif _can(db, actor, APPROVE):
        users = {actor.user_id} | authz.delegators(db, actor.user_id, APPROVE)
        mine_as = set(people.employees_of_users(db, users).values())
        if mine_as:
            clauses.append(ExpenseStep.approver_employee_id.in_(mine_as))
    if not clauses:
        return []
    rows = list(
        db.scalars(stmt.where(or_(*clauses)).order_by(ExpenseDocument.submitted_at).limit(200))
    )
    return [v for v in _views(db, actor, rows) if v.can_decide]


def decided_by_me(db: Session, actor: Actor) -> list[DocumentView]:
    ids = select(ExpenseStep.document_id).where(ExpenseStep.decided_by == actor.user_id)
    rows = list(
        db.scalars(
            select(ExpenseDocument)
            .where(ExpenseDocument.id.in_(ids))
            .order_by(ExpenseDocument.decided_at.desc().nulls_last())
            .limit(100)
        )
    )
    return _views(db, actor, rows)


def to_pay(db: Session, actor: Actor, kind: str) -> list[DocumentView]:
    if not _can(db, actor, REIMBURSE):
        return []
    rows = list(
        db.scalars(
            select(ExpenseDocument)
            .where(ExpenseDocument.status == "approved", ExpenseDocument.kind == kind)
            .order_by(ExpenseDocument.decided_at)
        )
    )
    return _views(db, actor, rows)


def payable_ids(db: Session, actor: Actor, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = db.scalars(select(ExpenseDocument).where(ExpenseDocument.id.in_(ids)))
    return {r.id for r in rows if can_pay(db, actor, r)}


def repayments_due(db: Session, actor: Actor) -> list[DocumentView]:
    if not _can(db, actor, REIMBURSE):
        return []
    rows = list(
        db.scalars(
            select(ExpenseDocument).where(
                ExpenseDocument.kind == "advance",
                ExpenseDocument.status == "reimbursed",
                ExpenseDocument.settled_at.is_(None),
            )
        )
    )
    return [v for v in _views(db, actor, rows) if v.outstanding and v.outstanding > 0]


def paid(db: Session, actor: Actor, limit: int = 50) -> list[DocumentView]:
    if not _can(db, actor, REIMBURSE):
        return []
    rows = list(
        db.scalars(
            select(ExpenseDocument)
            .where(ExpenseDocument.status == "reimbursed")
            .order_by(ExpenseDocument.paid_at.desc())
            .limit(limit)
        )
    )
    return _views(db, actor, rows)


# ------------------------------------------------------------------ for the ledger (Phase 10)


def queued_journals(db: Session, limit: int = 200) -> list[JournalView]:
    journals = list(
        db.scalars(
            select(ExpenseJournal)
            .where(ExpenseJournal.status == "queued")
            .order_by(ExpenseJournal.journal_date)
            .limit(limit)
        )
    )
    lines: dict[uuid.UUID, list[tuple[str, Decimal, Decimal]]] = defaultdict(list)
    for line in db.scalars(
        select(ExpenseJournalLine).where(
            ExpenseJournalLine.journal_id.in_([j.id for j in journals])
        )
    ):
        lines[line.journal_id].append((line.account, line.debit, line.credit))
    return [JournalView(j.id, j.journal_date, j.memo, j.status, lines[j.id]) for j in journals]
