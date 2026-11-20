"""File purposes for payroll (ADR 0021): generated payslips, and proof of rent for rent
relief. Both are `compensation` class."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.payroll.models import PayrollRun, Payslip, RentDeclaration


def _payslip(db: Session, entity_id: str) -> Resource | None:
    """Until the run is paid the payslip has no owner, so only organisation-wide payroll
    staff can open it; the employee can from payment on (ADR 0021)."""
    try:
        row = db.get(Payslip, uuid.UUID(entity_id))
    except ValueError:
        return None
    if row is None:
        return None
    run = db.get(PayrollRun, row.run_id)
    paid = run is not None and run.status == "paid"
    return Resource(owner_employee_id=row.employee_id if paid else None)


def _declaration(db: Session, entity_id: str) -> Resource | None:
    try:
        row = db.get(RentDeclaration, uuid.UUID(entity_id))
    except ValueError:
        return None
    return Resource(owner_employee_id=row.employee_id) if row else None


PAYSLIP = FilePurpose(
    key="payslip",
    label="Payslip",
    entity_type="payslip",
    allowed_types=frozenset({"application/pdf"}),
    max_bytes=2 * MB,
    attach_permission="payroll.run.approve",
    view_permission="payroll.payslip.read",
    # Pay records are kept six years after they are replaced (confirm).
    retention=timedelta(days=6 * 365),
    inline=True,
    classification="compensation",
    resolve=_payslip,
)

RENT_PROOF = FilePurpose(
    key="rent_proof",
    label="Proof of rent",
    entity_type="rent_declaration",
    allowed_types=frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"}),
    max_bytes=10 * MB,
    attach_permission="payroll.relief.declare",
    view_permission="payroll.relief.verify",
    retention=timedelta(days=6 * 365),
    inline=False,
    classification="compensation",
    resolve=_declaration,
)

PURPOSES = (PAYSLIP, RENT_PROOF)
