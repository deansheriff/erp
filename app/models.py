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
from app.core.numbering import NumberSequence
from app.files.models import FileLink, StorageSettings, StoredFile
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
from app.modules.hr.models import (
    ChangeRequest,
    Checklist,
    ChecklistTask,
    ChecklistTemplate,
    ChecklistTemplateTask,
    DocumentCategory,
    Employee,
    EmployeeDocument,
    EmployeeExit,
)
from app.modules.leave.models import (
    EntitlementRule,
    LeaveAdjustment,
    LeaveDecision,
    LeaveRequest,
    LeaveType,
    PublicHoliday,
)
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
    "ChangeRequest",
    "Checklist",
    "ChecklistTask",
    "ChecklistTemplate",
    "ChecklistTemplateTask",
    "CostCentre",
    "Delegation",
    "Department",
    "DocumentCategory",
    "Employee",
    "EmployeeDocument",
    "EmployeeExit",
    "EntitlementRule",
    "FileLink",
    "Grade",
    "Installation",
    "LeaveAdjustment",
    "LeaveDecision",
    "LeaveRequest",
    "LeaveType",
    "LoginAttempt",
    "MailOutbox",
    "MailSettings",
    "MfaRecoveryCode",
    "NumberSequence",
    "OrgState",
    "Organization",
    "PermissionRow",
    "Position",
    "PositionAssignment",
    "PublicHoliday",
    "Role",
    "RoleAssignment",
    "RoleGrant",
    "SecuritySettings",
    "StorageSettings",
    "StoredFile",
    "User",
    "UserSession",
    "UserToken",
]
