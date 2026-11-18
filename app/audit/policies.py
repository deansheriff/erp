"""Permissions for the audit log."""

from app.authz.permissions import ORG_ONLY, Permission

LOG_READ = Permission(
    "audit.log.read",
    "Read the audit log",
    "See every sign-in, change and denied action.",
    sensitive=True,
    scopes=ORG_ONLY,
)

PERMISSIONS = (LOG_READ,)
SOD_RULES = ()
