"""Permissions for roles, assignments and delegations."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

ROLE_READ = Permission(
    "authz.role.read", "View roles", "See roles and their permissions.", scopes=ORG_ONLY
)
ROLE_MANAGE = Permission(
    "authz.role.manage",
    "Manage roles",
    "Create, edit and delete roles.",
    sensitive=True,
    scopes=ORG_ONLY,
)
ASSIGNMENT_MANAGE = Permission(
    "authz.assignment.manage",
    "Assign roles",
    "Give people roles, including temporary ones.",
    sensitive=True,
    scopes=ORG_ONLY,
)
DELEGATION_MANAGE = Permission(
    "authz.delegation.manage",
    "Manage everyone's delegations",
    "See and revoke delegations between any two people.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (ROLE_READ, ROLE_MANAGE, ASSIGNMENT_MANAGE, DELEGATION_MANAGE)

SOD_RULES = (
    SodRule(
        key="authz.no_self_assignment",
        permission=ASSIGNMENT_MANAGE.key,
        conflict="subject_is_actor",
        message="You can't change your own roles. Ask another administrator.",
    ),
)
