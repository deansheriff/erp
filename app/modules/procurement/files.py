"""File purposes for procurement (ADR 0030): the supplier's own document behind a quote.
Procurement attaches it; anyone who may see RFQs may see it."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.procurement.models import Quote


def _quote(db: Session, entity_id: str) -> Resource | None:
    try:
        quote = db.get(Quote, uuid.UUID(entity_id))
    except ValueError:
        return None
    return Resource() if quote else None


SUPPLIER_QUOTE = FilePurpose(
    key="supplier_quote",
    label="Supplier's quote",
    entity_type="supplier_quote",
    allowed_types=frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"}),
    max_bytes=10 * MB,
    attach_permission="procurement.rfq.manage",
    view_permission="procurement.rfq.read",
    # Kept six years, like other purchasing records (confirm).
    retention=timedelta(days=6 * 365),
    inline=True,
    classification="directory",
    resolve=_quote,
)

PURPOSES = (SUPPLIER_QUOTE,)
