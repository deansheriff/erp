"""Permissions for mail settings."""

from app.authz.permissions import ORG_ONLY, Permission

MAIL_MANAGE = Permission(
    "system.mail.manage",
    "Manage mail settings",
    "Change the SMTP server and send test emails.",
    sensitive=True,
    scopes=ORG_ONLY,
)

PERMISSIONS = (MAIL_MANAGE,)
SOD_RULES = ()
