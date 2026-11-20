"""HR: people and their directory fields (ADR 0015, ADR 0017). The classed fields are in
records.py."""

import uuid
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.authz import service as authz
from app.core import numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.secrets import decrypt
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr.models import EMPLOYEE_STATUSES, Employee, EmployeeExit
from app.modules.hr.schemas import PayrollFacts, PersonInput, PersonRow, RemittanceDetails
from app.org import service as org
from app.org.models import Branch

EMPLOYEE_SEQUENCE = "employee"


def _check_branch(db: Session, branch_id: uuid.UUID | None) -> None:
    if branch_id is None:
        return
    branch = db.get(Branch, branch_id)
    if branch is None or not branch.is_active:
        raise ValidationFailed(field_errors={"branch_id": "Choose a branch."})


def _directory(row: Employee) -> dict[str, object]:
    return {
        "name": row.full_name,
        "work_email": row.work_email,
        "user_id": row.user_id,
        "branch_id": row.branch_id,
        "status": row.status,
    }


def _check_account(db: Session, user_id: uuid.UUID | None, own_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    if db.get(User, user_id) is None:
        raise ValidationFailed(field_errors={"user_id": "Choose a sign-in account."})
    taken = db.scalar(select(Employee.id).where(Employee.user_id == user_id))
    if taken is not None and taken != own_id:
        raise ValidationFailed(
            field_errors={"user_id": "This account already belongs to another person."}
        )


def _check_email(db: Session, email: str | None, own_id: uuid.UUID | None) -> str | None:
    if email is None:
        return None
    email = email.strip().lower()
    taken = db.scalar(select(Employee.id).where(Employee.work_email == email))
    if taken is not None and taken != own_id:
        raise ValidationFailed(field_errors={"work_email": "Another person has this email."})
    return email


def add_person(db: Session, actor: Actor, data: PersonInput) -> uuid.UUID:
    authz.require(db, actor, "hr.employee.create")
    email = _check_email(db, str(data.work_email) if data.work_email else None, None)
    _check_account(db, data.user_id, None)
    _check_branch(db, data.branch_id)
    row = Employee(
        employee_number=numbering.next_number(db, EMPLOYEE_SEQUENCE),
        first_name=data.first_name,
        last_name=data.last_name,
        other_names=data.other_names,
        work_email=email,
        user_id=data.user_id,
        branch_id=data.branch_id,
        status=data.status or "active",
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "hr.employee.create",
        entity_type="employee",
        entity_id=str(row.id),
        changes=diff_changes({}, {"employee_number": row.employee_number, **_directory(row)}),
    )
    return row.id


def update_person(db: Session, actor: Actor, employee_id: uuid.UUID, data: PersonInput) -> None:
    authz.require(db, actor, "hr.employee.update")
    row = get_person(db, actor, employee_id)
    if not engine.check(
        db, actor.user_id, "hr.employee.update", engine.Resource(owner_employee_id=row.id)
    ):
        raise NotFound()
    if row.user_id is not None and row.user_id == actor.user_id:
        raise PermissionDenied("Ask someone else in HR to change your own record.")
    email = _check_email(db, str(data.work_email) if data.work_email else None, row.id)
    _check_account(db, data.user_id, row.id)
    _check_branch(db, data.branch_id if data.branch_id != row.branch_id else None)
    if data.status is not None and row.status == "terminated":
        raise Conflict("This person has left. Their status can't be changed.")
    before = _directory(row)
    row.first_name, row.last_name, row.other_names = (
        data.first_name,
        data.last_name,
        data.other_names,
    )
    row.work_email, row.user_id, row.branch_id = email, data.user_id, data.branch_id
    if data.status is not None:
        row.status = data.status
    record_by(
        db,
        actor,
        "hr.employee.update",
        entity_type="employee",
        entity_id=str(row.id),
        changes=diff_changes(before, _directory(row)),
    )


def list_people(
    db: Session,
    actor: Actor,
    *,
    query: str | None = None,
    status: str | None = None,
    without_position: bool = False,
) -> list[PersonRow]:
    """People the actor may see (hr.employee.read, scoped), directory fields only."""
    stmt = select(Employee).where(
        engine.scope_filter(db, actor.user_id, "hr.employee.read", Employee)
    )
    if status in EMPLOYEE_STATUSES:
        stmt = stmt.where(Employee.status == status)
    if query and query.strip():
        like = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                func.lower(func.coalesce(Employee.other_names, "")).like(like),
                func.lower(func.coalesce(Employee.work_email, "")).like(like),
                func.lower(func.coalesce(Employee.employee_number, "")).like(like),
            )
        )
    rows = list(db.scalars(stmt.order_by(Employee.last_name, Employee.first_name).limit(1000)))
    held = org.positions_held(db, [r.id for r in rows])
    branches = branch_names(db)
    people = [
        PersonRow(
            id=r.id,
            full_name=r.full_name,
            first_name=r.first_name,
            last_name=r.last_name,
            work_email=r.work_email,
            status=r.status,
            has_account=r.user_id is not None,
            positions=held.get(r.id, []),
            employee_number=r.employee_number,
            branch=branches.get(r.branch_id) if r.branch_id else None,
        )
        for r in rows
    ]
    return [p for p in people if not p.positions] if without_position else people


def branch_names(db: Session) -> dict[uuid.UUID, str]:
    return dict(db.execute(select(Branch.id, Branch.name)).all())


def branch_choices(db: Session, keep: uuid.UUID | None = None) -> list[tuple[uuid.UUID, str]]:
    return [
        (b.id, b.name)
        for b in db.scalars(
            select(Branch).where(or_(Branch.is_active, Branch.id == keep)).order_by(Branch.name)
        )
    ]


def employee_of_user(db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(Employee.id).where(Employee.user_id == user_id))


def get_person(db: Session, actor: Actor, employee_id: uuid.UUID) -> Employee:
    """One person, if the actor may read them; otherwise not found (out of scope = 404)."""
    row = db.scalars(
        select(Employee).where(
            Employee.id == employee_id,
            engine.scope_filter(db, actor.user_id, "hr.employee.read", Employee),
        )
    ).one_or_none()
    if row is None:
        raise NotFound()
    return row


def names(db: Session, employee_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Display names for the org chart (directory class, visible to chart readers)."""
    if not employee_ids:
        return {}
    return {
        e.id: e.full_name for e in db.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    }


def assignable_people(db: Session) -> list[tuple[uuid.UUID, str, str | None]]:
    """People who can be put in a position (not terminated)."""
    return [
        (e.id, e.full_name, e.work_email)
        for e in db.scalars(
            select(Employee)
            .where(Employee.status != "terminated")
            .order_by(Employee.last_name, Employee.first_name)
        )
    ]


def accounts_without_person(
    db: Session, *, keep: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, str, str]]:
    """Sign-in accounts not yet linked to a person (plus `keep`, the account of the person
    being edited), for the person form."""
    linked = select(Employee.user_id).where(Employee.user_id.is_not(None))
    if keep is not None:
        linked = linked.where(Employee.user_id != keep)
    return [
        (u.id, u.full_name, u.email)
        for u in db.scalars(select(User).where(User.id.not_in(linked)).order_by(User.full_name))
    ]


def assign_to_position(
    db: Session,
    actor: Actor,
    *,
    position_id: uuid.UUID,
    employee_id: uuid.UUID,
    kind: str,
    start_date: date,
    end_date: date | None,
    reason: str,
) -> uuid.UUID:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise ValidationFailed(field_errors={"employee_id": "Choose a person."})
    if employee.status == "terminated":
        raise Conflict("This person has left the organisation.")
    return org.assign(
        db,
        actor,
        position_id=position_id,
        employee_id=employee_id,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
    )


# ------------------------------------------------------------------ for other modules


def hire_dates(db: Session, employee_ids: list[uuid.UUID]) -> dict[uuid.UUID, date | None]:
    """Hire dates for entitlement and service calculations (not for display)."""
    if not employee_ids:
        return {}
    return dict(
        db.execute(
            select(Employee.id, Employee.hire_date).where(Employee.id.in_(employee_ids))
        ).all()
    )


def status_and_account(db: Session, employee_id: uuid.UUID) -> tuple[str, uuid.UUID | None] | None:
    row = db.execute(
        select(Employee.status, Employee.user_id).where(Employee.id == employee_id)
    ).first()
    return (row[0], row[1]) if row else None


def current_ids(db: Session) -> list[uuid.UUID]:
    """Everyone who hasn't left."""
    return list(db.scalars(select(Employee.id).where(Employee.status != "terminated")))


def page_in_scope(
    db: Session, actor: Actor, permission: str, *, q: str = "", page: int = 1, size: int = 50
) -> tuple[list[Employee], int]:
    """Current people within the actor's scope of `permission`, searchable by name or
    number, one page at a time. Returns the page and the number of pages."""
    stmt = select(Employee).where(
        Employee.status != "terminated",
        engine.scope_filter(db, actor.user_id, permission, Employee),
    )
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                func.lower(func.coalesce(Employee.employee_number, "")).like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // size))
    rows = list(
        db.scalars(
            stmt.order_by(Employee.first_name, Employee.last_name)
            .offset((min(page, pages) - 1) * size)
            .limit(size)
        )
    )
    return rows, pages


def branches_of(db: Session, employee_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID | None]:
    """Each person's branch (for clocking rules)."""
    if not employee_ids:
        return {}
    return dict(
        db.execute(
            select(Employee.id, Employee.branch_id).where(Employee.id.in_(employee_ids))
        ).all()
    )


def accounts_of(db: Session, employee_ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """The sign-in account of each person who has one."""
    if not employee_ids:
        return {}
    rows = db.execute(
        select(Employee.id, Employee.user_id).where(
            Employee.id.in_(employee_ids), Employee.user_id.is_not(None)
        )
    ).all()
    return {employee_id: user_id for employee_id, user_id in rows if user_id is not None}


def employees_of_users(db: Session, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """The person record of each account that has one, by account."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(Employee.user_id, Employee.id).where(Employee.user_id.in_(user_ids))
    ).all()
    return {user_id: employee_id for user_id, employee_id in rows if user_id is not None}


def payroll_facts(db: Session, employee_ids: list[uuid.UUID]) -> dict[uuid.UUID, PayrollFacts]:
    """What payroll needs about each person: name and number for the payslip, the dates
    their employment starts and ends (for proration), and their PFA."""
    if not employee_ids:
        return {}
    leaving = dict(
        db.execute(
            select(EmployeeExit.employee_id, func.min(EmployeeExit.last_working_day))
            .where(
                EmployeeExit.employee_id.in_(employee_ids),
                EmployeeExit.status != "cancelled",
            )
            .group_by(EmployeeExit.employee_id)
        ).all()
    )
    return {
        e.id: PayrollFacts(
            name=e.full_name,
            number=e.employee_number,
            hire_date=e.hire_date,
            last_day=leaving.get(e.id),
            pfa=e.pension_pfa,
            state=e.state_of_residence,
        )
        for e in db.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    }


def remittance_details(
    db: Session, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RemittanceDetails]:
    """Decrypted identifiers for bank and statutory remittance files. Payroll checks
    `payroll.remittance.export` and audits each download (ADR 0022)."""
    if not employee_ids:
        return {}

    def plain(value: str | None) -> str | None:
        return decrypt(value) if value else None

    return {
        e.id: RemittanceDetails(
            tin=plain(e.tin_encrypted),
            rsa_pin=plain(e.rsa_pin_encrypted),
            nhf_number=plain(e.nhf_number_encrypted),
            bank_name=e.bank_name,
            account_number=plain(e.account_number_encrypted),
            account_name=e.account_name,
        )
        for e in db.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    }
