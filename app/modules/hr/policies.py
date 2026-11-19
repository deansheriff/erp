"""HR permissions (brief section 4.1, ADR 0017): directory-level employee permissions, one
read and one update permission per field class, and the HR processes."""

from app.authz.fields import FieldClass, read_permission, update_permission
from app.authz.permissions import ORG_ONLY, Permission, Scope

EMPLOYEE_READ = Permission(
    "hr.employee.read",
    "View people",
    "Names, positions, departments and work emails (the directory fields).",
)
EMPLOYEE_CREATE = Permission(
    "hr.employee.create", "Add people", "Add someone to the organisation.", scopes=ORG_ONLY
)
EMPLOYEE_UPDATE = Permission(
    "hr.employee.update",
    "Edit people",
    "Change directory details and status, link a sign-in account.",
)

_CLASS_TEXT = {
    FieldClass.PERSONAL: (
        "personal details",
        "Contact details, next of kin, date of birth and employment dates.",
    ),
    FieldClass.STATUTORY: ("statutory details", "NIN, TIN, pension and NHF numbers."),
    FieldClass.BANKING: ("bank details", "Bank account number and BVN."),
    FieldClass.COMPENSATION: ("pay documents", "Contracts and other pay documents."),
    FieldClass.SENSITIVE: ("sensitive documents", "Medical and other sensitive documents."),
}


def _class_permissions() -> tuple[Permission, ...]:
    result: list[Permission] = []
    for field_class, (noun, description) in _CLASS_TEXT.items():
        sensitive = field_class.masked_in_audit
        result.append(
            Permission(
                read_permission("hr.employee", field_class),
                f"View {noun}",
                description,
                sensitive=sensitive,
            )
        )
        result.append(
            Permission(
                update_permission("hr.employee", field_class),
                f"Change {noun}",
                description,
                sensitive=sensitive,
            )
        )
    return tuple(result)


PROFILE_REQUEST = Permission(
    "hr.profile.request_change",
    "Request changes to own details",
    "Ask HR to change your own statutory or bank details.",
    scopes=(Scope.SELF,),
)
CHECKLIST_MANAGE = Permission(
    "hr.checklist.manage",
    "Run onboarding and offboarding",
    "Start checklists and complete any task on them.",
    scopes=ORG_ONLY,
)
CHECKLIST_IT = Permission(
    "hr.checklist.it",
    "Complete IT checklist tasks",
    "Tick off the IT tasks on onboarding and offboarding checklists.",
    scopes=ORG_ONLY,
)
EXIT_MANAGE = Permission(
    "hr.exit.manage",
    "Record exits",
    "Record resignations and terminations; access ends after the last working day.",
    sensitive=True,
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "hr.settings.manage",
    "Manage HR settings",
    "Employee numbers, document categories and checklist templates.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    EMPLOYEE_READ,
    EMPLOYEE_CREATE,
    EMPLOYEE_UPDATE,
    *_class_permissions(),
    PROFILE_REQUEST,
    CHECKLIST_MANAGE,
    CHECKLIST_IT,
    EXIT_MANAGE,
    SETTINGS_MANAGE,
)
SOD_RULES = ()
