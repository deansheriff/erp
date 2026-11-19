"""File purposes for leave (ADR 0018): medical certificates are `sensitive` — the employee
attaches them, and only HR and the employee can open them."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.leave.models import LeaveRequest


def _request(db: Session, entity_id: str) -> Resource | None:
    try:
        request = db.get(LeaveRequest, uuid.UUID(entity_id))
    except ValueError:
        return None
    return Resource(owner_employee_id=request.employee_id) if request else None


LEAVE_CERTIFICATE = FilePurpose(
    key="leave_certificate",
    label="Medical certificate",
    entity_type="leave_request",
    allowed_types=frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"}),
    max_bytes=10 * MB,
    attach_permission="leave.request.create",
    view_permission="hr.employee.sensitive.read",
    # How long a replaced or deleted certificate is kept (confirm with the product owner).
    retention=timedelta(days=6 * 365),
    inline=False,
    classification="sensitive",
    resolve=_request,
)

PURPOSES = (LEAVE_CERTIFICATE,)
