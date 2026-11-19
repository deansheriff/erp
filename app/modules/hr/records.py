"""The HR record (ADR 0017): classed fields, read and changed only as far as the actor's
field-class permissions reach, and employees' change requests for statutory and banking
details. Hidden classes never leave this module."""

import json
import re
import uuid
from collections.abc import Mapping
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import MASK, diff_changes, record_by
from app.authz import engine
from app.authz import service as authz
from app.authz.engine import Resource
from app.authz.fields import FieldClass, read_permission, update_permission
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.numbering import NumberSequence
from app.core.secrets import decrypt, encrypt
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import fields as hr_fields
from app.modules.hr import service as people
from app.modules.hr.fields import BY_NAME, FIELDS, MASKED, SECTIONS, HrField
from app.modules.hr.models import ChangeRequest, Employee
from app.modules.hr.schemas import (
    ChangeRequestView,
    ClassAccess,
    EmployeeRecord,
    FieldValue,
    RequestChange,
    SectionView,
)
from app.org import service as org

PREFIX = "hr.employee"
RECORD_CLASSES = (
    FieldClass.PERSONAL,
    FieldClass.STATUTORY,
    FieldClass.BANKING,
    FieldClass.COMPENSATION,
    FieldClass.SENSITIVE,
)
REQUESTABLE = (FieldClass.STATUTORY, FieldClass.BANKING)
MIN_NOTE = 5
MAX_PADDING = 12


def resource(employee_id: uuid.UUID) -> Resource:
    return Resource(owner_employee_id=employee_id)


def access(db: Session, actor: Actor, employee_id: uuid.UUID) -> ClassAccess:
    res = resource(employee_id)
    read = {
        c
        for c in RECORD_CLASSES
        if engine.check(db, actor.user_id, read_permission(PREFIX, c), res)
    }
    update = {
        c
        for c in RECORD_CLASSES
        if engine.check(db, actor.user_id, update_permission(PREFIX, c), res)
    }
    is_self = people.employee_of_user(db, actor.user_id) == employee_id
    return ClassAccess(frozenset(read), frozenset(update), is_self)


def _get(row: Employee, f: HrField) -> str | date | None:
    raw = getattr(row, f.column)
    if raw is None:
        return None
    return decrypt(raw) if f.encrypted else raw


def _set(row: Employee, f: HrField, value: str | date | None) -> None:
    if f.encrypted:
        value = encrypt(str(value)) if value is not None else None
    setattr(row, f.column, value)


def _form_value(value: str | date | None) -> str:
    if value is None:
        return ""
    return value.isoformat() if isinstance(value, date) else str(value)


def get_record(db: Session, actor: Actor, employee_id: uuid.UUID) -> EmployeeRecord:
    """The record with only the classes the actor may read. Out of read scope is 404."""
    row = people.get_person(db, actor, employee_id)
    acc = access(db, actor, row.id)
    sections = []
    for key, title, field_class in SECTIONS:
        if field_class not in acc.read:
            continue
        values = []
        for f in (f for f in FIELDS if f.section == key):
            value = _get(row, f)
            values.append(
                FieldValue(f, _form_value(value), hr_fields.display(f, value), acc.can_edit(f))
            )
        sections.append(SectionView(key, title, field_class, values))
    user = db.get(User, row.user_id) if row.user_id else None
    manager_id = org.manager_of(db, row.id, clock.today())
    manager = db.get(Employee, manager_id) if manager_id else None
    branches = people.branch_names(db)
    return EmployeeRecord(
        id=row.id,
        employee_number=row.employee_number,
        full_name=row.full_name,
        first_name=row.first_name,
        last_name=row.last_name,
        other_names=row.other_names,
        work_email=row.work_email,
        status=row.status,
        branch_id=row.branch_id,
        branch=branches.get(row.branch_id) if row.branch_id else None,
        user_id=row.user_id,
        account_status=user.status if user else None,
        positions=org.positions_held(db, [row.id]).get(row.id, []),
        manager=manager.full_name if manager else None,
        access=acc,
        sections=sections,
    )


def blank_sections(db: Session, actor: Actor) -> list[SectionView]:
    """Empty sections for a new person: the classes the actor may set at all."""
    allowed = {
        c for c in RECORD_CLASSES if engine.check(db, actor.user_id, update_permission(PREFIX, c))
    }
    return [
        SectionView(
            key,
            title,
            field_class,
            [FieldValue(f, "", "", True) for f in FIELDS if f.section == key],
        )
        for key, title, field_class in SECTIONS
        if field_class in allowed
    ]


def _clean_all(submitted: Mapping[str, str]) -> tuple[dict[str, str | date | None], dict[str, str]]:
    cleaned: dict[str, str | date | None] = {}
    errors: dict[str, str] = {}
    for name, raw in submitted.items():
        try:
            cleaned[name] = hr_fields.clean(BY_NAME[name], raw)
        except ValueError as exc:
            errors[name] = exc.args[0] if exc.args else "Check this value."
    return cleaned, errors


def _cross_check(row: Employee, cleaned: Mapping[str, str | date | None]) -> dict[str, str]:
    def pick(name: str) -> object:
        return cleaned[name] if name in cleaned else getattr(row, name)

    start, end = pick("nysc_start"), pick("nysc_end")
    errors = {}
    if isinstance(start, date) and isinstance(end, date) and end < start:
        errors["nysc_end"] = "The end can't be before the start."
    hire, probation = pick("hire_date"), pick("probation_end")
    if isinstance(hire, date) and isinstance(probation, date) and probation < hire:
        errors["probation_end"] = "Probation can't end before the hire date."
    return errors


def update_fields(
    db: Session, actor: Actor, employee_id: uuid.UUID, submitted: Mapping[str, str]
) -> None:
    """Change classed fields. Only fields the actor may edit are accepted; anything else
    in the form is refused (forms only show editable fields)."""
    row = people.get_person(db, actor, employee_id)
    acc = access(db, actor, row.id)
    known = {k: v for k, v in submitted.items() if k in BY_NAME}
    if not known:
        return
    if any(not acc.can_edit(BY_NAME[name]) for name in known):
        raise PermissionDenied()
    cleaned, errors = _clean_all(known)
    errors |= _cross_check(row, cleaned)
    if errors:
        raise ValidationFailed(field_errors=errors)
    before = {name: _get(row, BY_NAME[name]) for name in cleaned}
    for name, value in cleaned.items():
        _set(row, BY_NAME[name], value)
    changes = diff_changes(before, cleaned, masked_fields=MASKED)
    if changes:
        record_by(
            db,
            actor,
            "hr.employee.update_details",
            entity_type="employee",
            entity_id=str(row.id),
            changes=changes,
        )


def configure_numbers(db: Session, actor: Actor, *, prefix: str, padding: str) -> None:
    """The format of new employee numbers. Numbers already given keep their format."""
    authz.require(db, actor, "hr.settings.manage")
    prefix = prefix.strip().upper()
    errors = {}
    if not re.fullmatch(r"[A-Z0-9/-]{0,20}", prefix):
        errors["prefix"] = "Use up to 20 letters, digits, hyphens or slashes."
    digits = int(padding) if padding.strip().isdigit() else 0
    if not 1 <= digits <= MAX_PADDING:
        errors["padding"] = f"Choose between 1 and {MAX_PADDING} digits."
    if errors:
        raise ValidationFailed(field_errors=errors)
    sequence = db.get(NumberSequence, people.EMPLOYEE_SEQUENCE)
    before = {"prefix": sequence.prefix, "padding": sequence.padding} if sequence else {}
    numbering.configure(db, people.EMPLOYEE_SEQUENCE, prefix=prefix, padding=digits)
    record_by(
        db,
        actor,
        "hr.settings.numbers",
        entity_type="number_sequence",
        entity_id=people.EMPLOYEE_SEQUENCE,
        changes=diff_changes(before, {"prefix": prefix, "padding": digits}),
    )


# ------------------------------------------------------------------ change requests


def request_change(
    db: Session, actor: Actor, field_class: FieldClass, submitted: Mapping[str, str]
) -> uuid.UUID:
    """An employee asks HR to change their own statutory or bank details."""
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or field_class not in REQUESTABLE:
        raise NotFound()
    if not engine.check(db, actor.user_id, "hr.profile.request_change", resource(employee_id)):
        raise PermissionDenied()
    row = db.get(Employee, employee_id)
    if row is None:
        raise NotFound()
    names = [f.name for f in hr_fields.fields_of(field_class)]
    cleaned, errors = _clean_all({n: submitted.get(n, "") for n in names})
    if errors:
        raise ValidationFailed(field_errors=errors)
    changed = {n: v for n, v in cleaned.items() if v != _get(row, BY_NAME[n])}
    if not changed:
        raise ValidationFailed(field_errors={"form": "Nothing has changed."})
    if db.scalar(
        select(ChangeRequest.id).where(
            ChangeRequest.employee_id == employee_id,
            ChangeRequest.field_class == field_class.value,
            ChangeRequest.status == "pending",
        )
    ):
        raise Conflict("You already have a request waiting for HR. Cancel it to send a new one.")
    request = ChangeRequest(
        employee_id=employee_id,
        field_class=field_class.value,
        field_names=",".join(changed),
        values_encrypted=encrypt(json.dumps({k: _form_value(v) for k, v in changed.items()})),
        requested_by=actor.user_id,
    )
    db.add(request)
    db.flush()
    record_by(
        db,
        actor,
        "hr.change_request.create",
        entity_type="employee",
        entity_id=str(employee_id),
        changes={name: {"from": MASK, "to": MASK} for name in changed},
    )
    return request.id


def can_request_change(db: Session, actor: Actor, employee_id: uuid.UUID | None) -> bool:
    if employee_id is None:
        return False
    return bool(engine.check(db, actor.user_id, "hr.profile.request_change", resource(employee_id)))


def _requested(request: ChangeRequest) -> dict[str, str]:
    values: dict[str, str] = json.loads(decrypt(request.values_encrypted))
    return values


def _decidable(db: Session, actor: Actor, request: ChangeRequest) -> bool:
    if request.status != "pending" or request.requested_by == actor.user_id:
        return False
    if people.employee_of_user(db, actor.user_id) == request.employee_id:
        return False
    permission = update_permission(PREFIX, FieldClass(request.field_class))
    return bool(engine.check(db, actor.user_id, permission, resource(request.employee_id)))


def _request_view(db: Session, actor: Actor, request: ChangeRequest) -> ChangeRequestView:
    employee = db.get(Employee, request.employee_id)
    if employee is None:
        raise NotFound()
    field_class = FieldClass(request.field_class)
    reveal = bool(
        engine.check(db, actor.user_id, read_permission(PREFIX, field_class), resource(employee.id))
    )
    changes = []
    for name, value in _requested(request).items():
        f = BY_NAME[name]
        current = hr_fields.display(f, _get(employee, f))
        requested = value
        if f.encrypted and not reveal:
            current = hr_fields.mask(current) if current else ""
            requested = hr_fields.mask(requested) if requested else ""
        changes.append(RequestChange(f.label, current, requested))
    names = {
        u.id: u.full_name
        for u in db.scalars(
            select(User).where(
                User.id.in_([i for i in (request.requested_by, request.decided_by) if i])
            )
        )
    }
    return ChangeRequestView(
        id=request.id,
        employee_id=employee.id,
        employee_name=employee.full_name,
        employee_number=employee.employee_number,
        field_class=request.field_class,
        status=request.status,
        requested_by=names.get(request.requested_by),
        requested_at=request.requested_at,
        decided_by=names.get(request.decided_by) if request.decided_by else None,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        changes=changes,
        can_decide=_decidable(db, actor, request),
    )


def list_requests(db: Session, actor: Actor, *, status: str = "pending") -> list[ChangeRequestView]:
    """Requests the actor may decide: those for classes they may update, in scope."""
    clauses = [
        (ChangeRequest.field_class == c.value)
        & engine.scope_filter(db, actor.user_id, update_permission(PREFIX, c), ChangeRequest)
        for c in REQUESTABLE
    ]
    stmt = select(ChangeRequest).where(clauses[0] | clauses[1])
    if status:
        stmt = stmt.where(ChangeRequest.status == status)
    rows = db.scalars(stmt.order_by(ChangeRequest.requested_at.desc()).limit(200))
    return [_request_view(db, actor, r) for r in rows]


def count_pending(db: Session, actor: Actor) -> int:
    return len(list_requests(db, actor))


def get_request(db: Session, actor: Actor, request_id: uuid.UUID) -> ChangeRequestView:
    request = db.get(ChangeRequest, request_id)
    if request is None:
        raise NotFound()
    own = request.requested_by == actor.user_id
    permission = update_permission(PREFIX, FieldClass(request.field_class))
    if not own and not engine.check(db, actor.user_id, permission, resource(request.employee_id)):
        raise NotFound()
    return _request_view(db, actor, request)


def my_requests(db: Session, actor: Actor) -> list[ChangeRequestView]:
    rows = db.scalars(
        select(ChangeRequest)
        .where(ChangeRequest.requested_by == actor.user_id)
        .order_by(ChangeRequest.requested_at.desc())
        .limit(20)
    )
    return [_request_view(db, actor, r) for r in rows]


def decide(
    db: Session, actor: Actor, request_id: uuid.UUID, *, approve: bool, note: str = ""
) -> None:
    request = db.scalars(
        select(ChangeRequest).where(ChangeRequest.id == request_id).with_for_update()
    ).one_or_none()
    if request is None:
        raise NotFound()
    get_request(db, actor, request_id)  # out of scope answers 404
    if request.status != "pending":
        raise Conflict("This request has already been decided.")
    if not _decidable(db, actor, request):
        raise PermissionDenied("You can't decide your own request.")
    note = note.strip()
    if not approve and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, so they know what to fix."})
    request.status = "approved" if approve else "rejected"
    request.decided_by, request.decided_at = actor.user_id, clock.now()
    request.decision_note = note or None
    employee = db.get(Employee, request.employee_id)
    if employee is None:
        raise NotFound()
    if approve:
        values = _requested(request)
        cleaned, errors = _clean_all(values)
        if errors:  # a field's rules changed since the request was made
            raise ValidationFailed(field_errors={"form": "This request no longer passes checks."})
        before = {n: _get(employee, BY_NAME[n]) for n in cleaned}
        for name, value in cleaned.items():
            _set(employee, BY_NAME[name], value)
        record_by(
            db,
            actor,
            "hr.employee.update_details",
            entity_type="employee",
            entity_id=str(employee.id),
            reason="Change request approved",
            changes=diff_changes(before, cleaned, masked_fields=MASKED),
        )
    record_by(
        db,
        actor,
        "hr.change_request.approve" if approve else "hr.change_request.reject",
        entity_type="employee",
        entity_id=str(employee.id),
        reason=note or None,
    )


def cancel_request(db: Session, actor: Actor, request_id: uuid.UUID) -> None:
    request = db.get(ChangeRequest, request_id)
    if request is None or request.requested_by != actor.user_id:
        raise NotFound()
    if request.status != "pending":
        raise Conflict("This request has already been decided.")
    request.status = "cancelled"
    request.decided_at = clock.now()
    record_by(
        db,
        actor,
        "hr.change_request.cancel",
        entity_type="employee",
        entity_id=str(request.employee_id),
    )
