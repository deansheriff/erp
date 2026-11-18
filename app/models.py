"""Imports every model module so Base.metadata is complete (Alembic, schema tests).

Add each new module's models here.
"""

from app.audit.models import AuditLog
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
    "Installation",
    "LoginAttempt",
    "MailOutbox",
    "MailSettings",
    "MfaRecoveryCode",
    "Organization",
    "SecuritySettings",
    "User",
    "UserSession",
    "UserToken",
]
