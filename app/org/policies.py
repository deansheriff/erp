"""Permissions for the organisation record and its structure (ADR 0015)."""

from app.authz.permissions import ORG_ONLY, Permission

BRANDING_MANAGE = Permission(
    "system.branding.manage",
    "Manage organisation details and branding",
    "Legal details, logo and document colours.",
    scopes=ORG_ONLY,
)
STRUCTURE_READ = Permission(
    "org.structure.read",
    "View the organisation chart",
    "Departments, positions, branches, cost centres, grades and who holds each position.",
    scopes=ORG_ONLY,
)
STRUCTURE_MANAGE = Permission(
    "org.structure.manage",
    "Manage the organisation structure",
    "Create and change departments, positions, branches, cost centres and grades.",
    scopes=ORG_ONLY,
)
ASSIGNMENT_MANAGE = Permission(
    "org.assignment.manage",
    "Assign people to positions",
    "Put people in positions as primary, acting or interim holders. This changes who "
    "manages whom, and so who can see and approve what.",
    sensitive=True,
    scopes=ORG_ONLY,
)

PERMISSIONS = (BRANDING_MANAGE, STRUCTURE_READ, STRUCTURE_MANAGE, ASSIGNMENT_MANAGE)
SOD_RULES = ()
