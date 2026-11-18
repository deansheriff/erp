"""Imports every model module so Base.metadata is complete (Alembic, schema tests).

Add each new module's models here.
"""

from app.audit.models import AuditLog
from app.authz.models import (
    AuthzState,
    Delegation,
    PermissionRow,
    Role,
    RoleAssignment,
    RoleGrant,
)
from app.identity.models import (
    ApiKey,
    Installation,
    LoginAttempt,
    MfaRecoveryCode,
    SecuritySettings,
    User,
    UserSession,
    UserToken,
)
from app.mail.models import MailOutbox, MailSettings
from app.org.models import Organization

__all__ = [
    "ApiKey",
    "AuditLog",
    "AuthzState",
    "Delegation",
    "Installation",
    "LoginAttempt",
    "MailOutbox",
    "MailSettings",
    "MfaRecoveryCode",
    "Organization",
    "PermissionRow",
    "Role",
    "RoleAssignment",
    "RoleGrant",
    "SecuritySettings",
    "User",
    "UserSession",
    "UserToken",
]
