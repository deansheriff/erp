"""Permissions for the organisation record. Phase 4 adds departments and positions."""

from app.authz.permissions import ORG_ONLY, Permission

BRANDING_MANAGE = Permission(
    "system.branding.manage",
    "Manage organisation details and branding",
    "Legal details, logo and document colours.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (BRANDING_MANAGE,)
SOD_RULES = ()
