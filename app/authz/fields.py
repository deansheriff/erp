"""Field-level sensitivity (brief section 4.1).

Each sensitive resource maps its fields to classes. Reading a class needs the
permission `<resource>.<class>.read` (for example `hr.employee.banking.read`); the
`directory` class needs only `<resource>.read`. Hidden fields are removed from the data,
never sent and hidden in the browser.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.authz.engine import Resource, check


class FieldClass(StrEnum):
    DIRECTORY = "directory"  # name, job title, department, work email, photo
    PERSONAL = "personal"  # phone, address, date of birth, next of kin
    COMPENSATION = "compensation"  # salary, allowances, grade
    BANKING = "banking"  # account number, BVN
    STATUTORY = "statutory"  # NIN, TIN, pension PIN, NHF number
    SENSITIVE = "sensitive"  # medical, disciplinary, grievance

    @property
    def masked_in_audit(self) -> bool:
        return self in {
            FieldClass.COMPENSATION,
            FieldClass.BANKING,
            FieldClass.STATUTORY,
            FieldClass.SENSITIVE,
        }


def read_permission(resource_prefix: str, field_class: FieldClass) -> str:
    if field_class is FieldClass.DIRECTORY:
        return f"{resource_prefix}.read"
    return f"{resource_prefix}.{field_class.value}.read"


def update_permission(resource_prefix: str, field_class: FieldClass) -> str:
    if field_class is FieldClass.DIRECTORY:
        return f"{resource_prefix}.update"
    return f"{resource_prefix}.{field_class.value}.update"


def readable_classes(
    db: Session,
    user_id: Any,
    resource_prefix: str,
    resource: Resource,
    classes: set[FieldClass],
) -> set[FieldClass]:
    """The classes this person may read on this record."""
    return {c for c in classes if check(db, user_id, read_permission(resource_prefix, c), resource)}


def redact(
    data: Mapping[str, Any], field_classes: Mapping[str, FieldClass], allowed: set[FieldClass]
) -> dict[str, Any]:
    """Drop every field whose class isn't allowed. Fields without a declared class are
    treated as `sensitive`, so forgetting to classify a field hides it rather than leaks it."""
    return {
        name: value
        for name, value in data.items()
        if field_classes.get(name, FieldClass.SENSITIVE) in allowed
    }


def masked_fields(field_classes: Mapping[str, FieldClass]) -> set[str]:
    """Fields whose values the audit log must never record."""
    return {name for name, cls in field_classes.items() if cls.masked_in_audit}
