"""Rent relief (ADR 0021): the employee declares a tax year's rent with proof; payroll
uses it only once HR or Payroll has verified it. Nobody verifies their own."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.money import parse_money
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll.files import RENT_PROOF
from app.modules.payroll.models import RentDeclaration

DECLARE = "payroll.relief.declare"
VERIFY = "payroll.relief.verify"
MIN_NOTE = 5


@dataclass(frozen=True, slots=True)
class DeclarationView:
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    tax_year: int
    annual_rent: Decimal
    proof_file_id: uuid.UUID | None
    status: str
    created_at: datetime
    decided_by: str | None
    note: str | None
    can_decide: bool = False


def declare(
    db: Session,
    actor: Actor,
    *,
    tax_year: int,
    annual_rent: str,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
) -> uuid.UUID:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not engine.check(
        db, actor.user_id, DECLARE, Resource(owner_employee_id=employee_id)
    ):
        raise PermissionDenied()
    this_year = clock.today().year
    errors = {}
    if tax_year not in {this_year, this_year - 1}:
        errors["tax_year"] = "Choose this year or last year."
    rent = parse_money(annual_rent, "annual_rent", blank=True)
    if not rent:
        errors["annual_rent"] = "Enter the rent you paid for the year."
    if not data:
        errors["file"] = "Attach proof, e.g. your tenancy agreement or rent receipt."
    if errors:
        raise ValidationFailed(field_errors=errors)
    clash = db.scalar(
        select(RentDeclaration.id).where(
            RentDeclaration.employee_id == employee_id,
            RentDeclaration.tax_year == tax_year,
            RentDeclaration.status != "rejected",
        )
    )
    if clash:
        raise Conflict("You've already declared rent for this year.")
    row = RentDeclaration(
        employee_id=employee_id, tax_year=tax_year, annual_rent=rent, declared_by=actor.user_id
    )
    db.add(row)
    db.flush()
    row.proof_file_id = files.upload(
        db,
        actor,
        purpose_key=RENT_PROOF.key,
        entity_id=str(row.id),
        filename=filename,
        declared_mime=declared_mime,
        data=data,
    )
    record_by(
        db,
        actor,
        "payroll.relief.declare",
        entity_type="rent_declaration",
        entity_id=str(row.id),
        changes=diff_changes({}, {"tax_year": tax_year, "rent": "set"}, masked_fields=("rent",)),
    )
    return row.id


def _may_decide(db: Session, actor: Actor, row: RentDeclaration) -> bool:
    if row.status != "pending" or not engine.check(db, actor.user_id, VERIFY):
        return False
    facts = people.status_and_account(db, row.employee_id)
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    return not sod.violations(
        catalogue.sod_rules(),
        VERIFY,
        actor.user_id,
        {"owner_user_id": facts[1] if facts else None},
        held,
    )


def decide(
    db: Session, actor: Actor, declaration_id: uuid.UUID, *, verify: bool, note: str = ""
) -> None:
    row = db.scalars(
        select(RentDeclaration).where(RentDeclaration.id == declaration_id).with_for_update()
    ).one_or_none()
    if row is None or not engine.check(db, actor.user_id, VERIFY):
        raise NotFound()
    if row.status != "pending":
        raise Conflict("This declaration has already been decided.")
    if not _may_decide(db, actor, row):
        raise PermissionDenied("You can't verify your own rent declaration.")
    note = note.strip()
    if not verify and len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say why, so they know what to send."})
    row.status = "verified" if verify else "rejected"
    row.decided_by, row.decided_at, row.note = actor.user_id, clock.now(), note or None
    record_by(
        db,
        actor,
        f"payroll.relief.{'verify' if verify else 'reject'}",
        entity_type="rent_declaration",
        entity_id=str(row.id),
        reason=note or None,
        changes={"status": {"from": "pending", "to": row.status}},
    )


def verified_rent(
    db: Session, employee_ids: list[uuid.UUID], tax_year: int
) -> dict[uuid.UUID, Decimal]:
    if not employee_ids:
        return {}
    return {
        row.employee_id: row.annual_rent
        for row in db.scalars(
            select(RentDeclaration).where(
                RentDeclaration.employee_id.in_(employee_ids),
                RentDeclaration.tax_year == tax_year,
                RentDeclaration.status == "verified",
            )
        )
    }


def _views(db: Session, actor: Actor, rows: list[RentDeclaration]) -> list[DeclarationView]:
    names = people.names(db, {r.employee_id for r in rows})
    deciders = (
        dict(
            db.execute(
                select(User.id, User.full_name).where(
                    User.id.in_({r.decided_by for r in rows if r.decided_by})
                )
            ).all()
        )
        if rows
        else {}
    )
    return [
        DeclarationView(
            id=r.id,
            employee_id=r.employee_id,
            employee_name=names.get(r.employee_id, ""),
            tax_year=r.tax_year,
            annual_rent=r.annual_rent,
            proof_file_id=r.proof_file_id,
            status=r.status,
            created_at=r.created_at,
            decided_by=deciders.get(r.decided_by) if r.decided_by else None,
            note=r.note,
            can_decide=_may_decide(db, actor, r),
        )
        for r in rows
    ]


def mine(db: Session, actor: Actor) -> list[DeclarationView]:
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None:
        return []
    rows = list(
        db.scalars(
            select(RentDeclaration)
            .where(RentDeclaration.employee_id == employee_id)
            .order_by(RentDeclaration.tax_year.desc(), RentDeclaration.created_at.desc())
        )
    )
    return _views(db, actor, rows)


def queue(db: Session, actor: Actor, status: str) -> list[DeclarationView]:
    if not engine.check(db, actor.user_id, VERIFY):
        raise PermissionDenied()
    rows = list(
        db.scalars(
            select(RentDeclaration)
            .where(RentDeclaration.status == status)
            .order_by(RentDeclaration.created_at)
            .limit(200)
        )
    )
    return _views(db, actor, rows)
