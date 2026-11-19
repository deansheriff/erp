"""Invitations from the HR record, exits and automatic access revocation (ADR 0017)."""

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import service as authz
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity import service as identity
from app.identity.models import User
from app.identity.schemas import Actor, InviteInput
from app.modules.hr import checklists
from app.modules.hr import service as people
from app.modules.hr.models import EXIT_KINDS, Employee, EmployeeExit
from app.modules.hr.schemas import ExitView
from app.org import service as org

MIN_REASON = 5
LIVE = ("scheduled", "revoked")


def _view(db: Session, row: EmployeeExit) -> ExitView:
    recorder = db.get(User, row.recorded_by) if row.recorded_by else None
    return ExitView(
        id=row.id,
        kind=row.kind,
        reason=row.reason,
        notice_date=row.notice_date,
        last_working_day=row.last_working_day,
        final_pay_due=row.final_pay_due,
        status=row.status,
        access_ends=checklists.access_end(row.last_working_day),
        recorded_by=recorder.full_name if recorder else None,
        revoked_at=row.revoked_at,
    )


def exit_for(db: Session, employee_id: uuid.UUID) -> ExitView | None:
    row = db.scalars(
        select(EmployeeExit).where(
            EmployeeExit.employee_id == employee_id, EmployeeExit.status.in_(LIVE)
        )
    ).one_or_none()
    return _view(db, row) if row else None


def invite(db: Session, actor: Actor, employee_id: uuid.UUID) -> uuid.UUID:
    """Send the sign-in invitation, link the new account and give it the Employee role."""
    authz.require(db, actor, "identity.user.invite")
    row = people.get_person(db, actor, employee_id)
    if row.status == "terminated":
        raise Conflict("This person has left the organisation.")
    if row.user_id is not None:
        raise Conflict("This person already has a sign-in account.")
    if not row.work_email:
        raise ValidationFailed(field_errors={"form": "Add a work email before inviting them."})
    user_id = identity.invite_user(
        db, actor, InviteInput(full_name=row.full_name, email=row.work_email)
    )
    row.user_id = user_id
    authz.give_employee_role(db, actor, user_id)
    record_by(
        db,
        actor,
        "hr.employee.invite",
        entity_type="employee",
        entity_id=str(row.id),
        changes={"user_id": {"from": None, "to": str(user_id)}},
    )
    return user_id


def _exit_errors(
    kind: str, reason: str, notice_date: date | None, last_working_day: date | None
) -> dict[str, str]:
    errors = {}
    if kind not in EXIT_KINDS:
        errors["kind"] = "Choose resignation or termination."
    if len(reason) < MIN_REASON:
        errors["reason"] = "Give the reason in a few words."
    if notice_date is None:
        errors["notice_date"] = "Enter the date notice was given."
    if last_working_day is None:
        errors["last_working_day"] = "Enter the last working day."
    elif notice_date is not None and last_working_day < notice_date:
        errors["last_working_day"] = "The last day can't be before the notice."
    return errors


def record_exit(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    kind: str,
    reason: str,
    notice_date: date | None,
    last_working_day: date | None,
    final_pay_due: bool,
) -> uuid.UUID:
    authz.require(db, actor, "hr.exit.manage")
    row = people.get_person(db, actor, employee_id)
    if row.user_id is not None and row.user_id == actor.user_id:
        raise PermissionDenied("Ask someone else to record your own exit.")
    if row.status == "terminated":
        raise Conflict("This person has already left.")
    if db.scalar(
        select(EmployeeExit.id).where(
            EmployeeExit.employee_id == row.id, EmployeeExit.status.in_(LIVE)
        )
    ):
        raise Conflict("An exit is already recorded for this person.")
    reason = reason.strip()
    errors = _exit_errors(kind, reason, notice_date, last_working_day)
    if errors or notice_date is None or last_working_day is None:
        raise ValidationFailed(field_errors=errors)
    if row.user_id is not None:
        try:
            authz.ensure_not_last_super_admin(db, row.user_id)
        except Conflict as exc:
            raise Conflict(
                "This person is the last active Super Administrator. "
                "Give the role to someone else first."
            ) from exc
    exit_row = EmployeeExit(
        employee_id=row.id,
        kind=kind,
        reason=reason,
        notice_date=notice_date,
        last_working_day=last_working_day,
        final_pay_due=final_pay_due,
        recorded_by=actor.user_id,
    )
    db.add(exit_row)
    db.flush()
    record_by(
        db,
        actor,
        "hr.exit.record",
        entity_type="employee",
        entity_id=str(row.id),
        reason=reason,
        changes=diff_changes(
            {},
            {
                "kind": kind,
                "notice_date": notice_date.isoformat(),
                "last_working_day": last_working_day.isoformat(),
                "final_pay_due": final_pay_due,
            },
        ),
    )
    if checklists.choose_template(db, "offboarding", row.id) is not None:
        checklists.start(
            db,
            actor,
            row.id,
            kind="offboarding",
            anchor_date=last_working_day,
            exit_id=exit_row.id,
        )
    if checklists.access_end(last_working_day) <= clock.now():
        _revoke(db, exit_row)
    return exit_row.id


def cancel_exit(db: Session, actor: Actor, exit_id: uuid.UUID) -> uuid.UUID:
    authz.require(db, actor, "hr.exit.manage")
    row = db.scalars(
        select(EmployeeExit).where(EmployeeExit.id == exit_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFound()
    people.get_person(db, actor, row.employee_id)
    if row.status != "scheduled":
        raise Conflict("Access has already been revoked, so this exit can't be cancelled.")
    row.status, row.cancelled_at, row.cancelled_by = "cancelled", clock.now(), actor.user_id
    checklists.cancel_for_exit(db, actor, row.id)
    record_by(db, actor, "hr.exit.cancel", entity_type="employee", entity_id=str(row.employee_id))
    return row.employee_id


def _revoke(db: Session, row: EmployeeExit) -> None:
    employee = db.get(Employee, row.employee_id)
    if employee is None:
        return
    why = f"Left the organisation ({row.kind}), last working day {row.last_working_day}"
    before = employee.status
    employee.status = "terminated"
    org.end_assignments_for_exit(db, employee.id, last_day=row.last_working_day, reason=why)
    if employee.user_id is not None:
        identity.deactivate_for_exit(db, employee.user_id, why)
    row.status, row.revoked_at = "revoked", clock.now()
    record_by(
        db,
        None,
        "hr.exit.revoke",
        entity_type="employee",
        entity_id=str(employee.id),
        reason=why,
        changes={"status": {"from": before, "to": "terminated"}},
    )


def revoke_due(db: Session, now: datetime | None = None) -> int:
    """Revoke access for every scheduled exit whose last working day has ended (run by
    the beat task every 15 minutes). Rows locked by another worker are skipped."""
    today = clock.local_date(now or clock.now())
    rows = list(
        db.scalars(
            select(EmployeeExit)
            .where(EmployeeExit.status == "scheduled", EmployeeExit.last_working_day < today)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    )
    for row in rows:
        _revoke(db, row)
    return len(rows)
