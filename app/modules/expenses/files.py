"""File purposes for expenses (ADR 0020): a receipt per claim line. Anyone who may see
the claim may see its receipts; the claimant attaches them."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.expenses.models import ExpenseDocument, ExpenseLine


def _line(db: Session, entity_id: str) -> Resource | None:
    try:
        line = db.get(ExpenseLine, uuid.UUID(entity_id))
    except ValueError:
        return None
    document = db.get(ExpenseDocument, line.document_id) if line else None
    return Resource(owner_employee_id=document.employee_id) if document else None


EXPENSE_RECEIPT = FilePurpose(
    key="expense_receipt",
    label="Receipt",
    entity_type="expense_line",
    allowed_types=frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"}),
    max_bytes=10 * MB,
    attach_permission="expenses.claim.create",
    view_permission="expenses.claim.read",
    # Replaced receipts are kept six years, like other financial records (confirm).
    retention=timedelta(days=6 * 365),
    inline=True,
    classification="directory",
    resolve=_line,
)

PURPOSES = (EXPENSE_RECEIPT,)
