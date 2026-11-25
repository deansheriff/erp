"""The file purpose registry (ADR 0016). Each package declares its purposes in its own
`files.py` as `PURPOSES`; this module collects them. Unregistered purposes are refused."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from importlib import import_module

from sqlalchemy.orm import Session

from app.authz.engine import Resource

# Imported by name so this core package never imports a business module directly.
PURPOSE_MODULES = (
    "app.org.files",
    "app.modules.hr.files",
    "app.modules.leave.files",
    "app.modules.attendance.files",
    "app.modules.expenses.files",
    "app.modules.payroll.files",
    "app.modules.finance.files",
)

KB = 1024
MB = 1024 * KB

# Resolves an entity ID to the facts authorisation needs, or None if it doesn't exist.
EntityResolver = Callable[[Session, str], Resource | None]


@dataclass(frozen=True, slots=True)
class FilePurpose:
    key: str
    label: str
    entity_type: str
    allowed_types: frozenset[str]  # detected MIME types
    max_bytes: int
    attach_permission: str
    # None means any signed-in person may view files with this purpose.
    view_permission: str | None
    # How long a deleted or replaced file is kept before the retention job removes it.
    retention: timedelta
    inline: bool  # may be shown in the browser rather than downloaded
    classification: str  # field class (app/authz/fields.py)
    resolve: EntityResolver
    # Set for files that must not be kept: deleted this long after upload, live or not.
    expires_after: timedelta | None = None

    def entity_uuid(self, entity_id: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(entity_id)
        except ValueError:
            return None


@cache
def purposes() -> dict[str, FilePurpose]:
    found: dict[str, FilePurpose] = {}
    for name in PURPOSE_MODULES:
        for purpose in import_module(name).PURPOSES:
            if purpose.key in found:
                raise ValueError(f"File purpose {purpose.key} is declared twice")
            found[purpose.key] = purpose
    return found


def get(key: str) -> FilePurpose | None:
    return purposes().get(key)


def largest_upload() -> int:
    return max((p.max_bytes for p in purposes().values()), default=0)
