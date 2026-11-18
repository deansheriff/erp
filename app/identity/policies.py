"""Permissions for people's accounts and security settings."""

from app.authz.permissions import ORG_ONLY, Permission

USER_READ = Permission(
    "identity.user.read", "View user accounts", "See everyone who can sign in.", scopes=ORG_ONLY
)
USER_INVITE = Permission(
    "identity.user.invite", "Invite users", "Send invitations to new people.", scopes=ORG_ONLY
)
USER_MANAGE = Permission(
    "identity.user.manage",
    "Manage user accounts",
    "Resend invitations, suspend, reactivate and unlock accounts.",
    sensitive=True,
    scopes=ORG_ONLY,
)
SECURITY_MANAGE = Permission(
    "system.security.manage",
    "Manage security settings",
    "Session, password and lockout rules, and who is a super admin.",
    sensitive=True,
    scopes=ORG_ONLY,
)

PERMISSIONS = (USER_READ, USER_INVITE, USER_MANAGE, SECURITY_MANAGE)
SOD_RULES = ()
