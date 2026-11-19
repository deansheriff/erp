"""HR permissions. Phase 4 declares the directory-level employee permissions; Phase 6
adds one read and one update permission per field class (brief section 4.1)."""

from app.authz.permissions import ORG_ONLY, Permission

EMPLOYEE_READ = Permission(
    "hr.employee.read",
    "View people",
    "Names, positions, departments and work emails (the directory fields).",
)
EMPLOYEE_CREATE = Permission(
    "hr.employee.create", "Add people", "Add someone to the organisation chart.", scopes=ORG_ONLY
)
EMPLOYEE_UPDATE = Permission(
    "hr.employee.update",
    "Edit people",
    "Change directory details and link a sign-in account.",
)

PERMISSIONS = (EMPLOYEE_READ, EMPLOYEE_CREATE, EMPLOYEE_UPDATE)
SOD_RULES = ()
