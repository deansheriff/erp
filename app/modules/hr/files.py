"""File purposes for employee documents (ADR 0017): one per field class, so attaching and
viewing a document needs that class's update and read permission on the employee."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.authz.fields import FieldClass, read_permission, update_permission
from app.files.purposes import MB, FilePurpose
from app.modules.hr.models import DOCUMENT_CLASSES, Employee

DOCUMENT_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
# How long a replaced or deleted employee document is kept (confirm with the product owner).
DOCUMENT_RETENTION = timedelta(days=6 * 365)


def _employee(db: Session, entity_id: str) -> Resource | None:
    try:
        employee_id = uuid.UUID(entity_id)
    except ValueError:
        return None
    return Resource(owner_employee_id=employee_id) if db.get(Employee, employee_id) else None


def _purpose(field_class: FieldClass) -> FilePurpose:
    return FilePurpose(
        key=f"employee_document_{field_class.value}",
        label=f"Employee document ({field_class.value})",
        entity_type="employee",
        allowed_types=DOCUMENT_TYPES,
        max_bytes=10 * MB,
        attach_permission=update_permission("hr.employee", field_class),
        view_permission=read_permission("hr.employee", field_class),
        retention=DOCUMENT_RETENTION,
        inline=False,
        classification=field_class.value,
        resolve=_employee,
    )


DOCUMENT_PURPOSES = {c: _purpose(FieldClass(c)) for c in DOCUMENT_CLASSES}
PURPOSES = tuple(DOCUMENT_PURPOSES.values())
