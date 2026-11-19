"""Permissions for file administration. Viewing and attaching files is governed by each
purpose's own permissions on the entity the file belongs to (ADR 0016)."""

from app.authz.permissions import ORG_ONLY, Permission

FILE_MANAGE = Permission(
    "files.file.manage",
    "Manage files",
    "See every file's details and scan state, place legal holds and delete files. "
    "Doesn't grant access to file contents.",
    sensitive=True,
    scopes=ORG_ONLY,
)
STORAGE_MANAGE = Permission(
    "system.storage.manage",
    "Manage storage settings",
    "Change the organisation's storage quota.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (FILE_MANAGE, STORAGE_MANAGE)
SOD_RULES = ()
