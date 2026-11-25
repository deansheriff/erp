"""File purposes for finance (ADR 0026): imported bank statements, kept for six years
(confirm). They hold account numbers, so they are `banking` class and every download is
audited."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.finance.models import BankAccount

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _bank_account(db: Session, entity_id: str) -> Resource | None:
    try:
        row = db.get(BankAccount, uuid.UUID(entity_id))
    except ValueError:
        return None
    return Resource() if row else None


BANK_STATEMENT = FilePurpose(
    key="bank_statement",
    label="Bank statement",
    entity_type="bank_account",
    allowed_types=frozenset({"text/plain", XLSX}),
    max_bytes=10 * MB,
    attach_permission="finance.bank.reconcile",
    view_permission="finance.bank.read",
    retention=timedelta(days=6 * 365),
    inline=False,
    classification="banking",
    resolve=_bank_account,
    expires_after=timedelta(days=6 * 365),
)

PURPOSES = (BANK_STATEMENT,)
