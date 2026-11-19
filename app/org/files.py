"""File purposes owned by the organisation record (ADR 0016)."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.org.models import Organization


def _organisation(db: Session, entity_id: str) -> Resource | None:
    org_id = db.scalar(select(Organization.id))
    return Resource() if org_id is not None and str(org_id) == entity_id else None


ORGANIZATION_LOGO = FilePurpose(
    key="organization_logo",
    label="Organisation logo",
    entity_type="organization",
    allowed_types=frozenset({"image/png", "image/jpeg", "image/webp", "image/svg+xml"}),
    max_bytes=2 * MB,
    attach_permission="system.branding.manage",
    view_permission=None,  # the logo appears on every document and page
    # How long a replaced or deleted logo is kept (confirm with the product owner).
    retention=timedelta(days=365),
    inline=True,
    classification="directory",
    resolve=_organisation,
)

PURPOSES = (ORGANIZATION_LOGO,)
