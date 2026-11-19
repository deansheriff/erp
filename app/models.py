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
from app.modules.hr.models import Employee
from app.org.models import (
    Branch,
    CostCentre,
    Department,
    Grade,
    Organization,
    OrgState,
    Position,
    PositionAssignment,
)

__all__ = [
    "ApiKey",
    "AuditLog",
    "AuthzState",
    "Branch",
    "CostCentre",
    "Delegation",
    "Department",
    "Employee",
    "Grade",
    "Installation",
    "LoginAttempt",
    "MailOutbox",
    "MailSettings",
    "MfaRecoveryCode",
    "OrgState",
    "Organization",
    "PermissionRow",
    "Position",
    "PositionAssignment",
    "Role",
    "RoleAssignment",
    "RoleGrant",
    "SecuritySettings",
    "User",
    "UserSession",
    "UserToken",
]
