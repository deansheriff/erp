"""Role templates (read-only). Each installation gets editable copies in the `roles` table.

Templates for business roles (Line Manager, Payroll Officer, …) start empty: each module
adds its template grants in its own phase, so nothing is granted before it exists.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.authz import catalogue
from app.authz.permissions import Permission, Scope

GrantRule = Callable[[Permission], Scope | None]


def _everything(_: Permission) -> Scope:
    return Scope.ORGANIZATION


def _all_but_system(p: Permission) -> Scope | None:
    """Organisation Administrator: everything except technical settings (SMTP, branding,
    security). Confirmed by the product owner on 2026-11-18."""
    return None if p.module == "system" else p.scopes[-1]  # the widest scope it allows


def _read_everything(p: Permission) -> Scope | None:
    """Auditor: read-only access to everything, sensitive fields included (2026-11-18)."""
    return Scope.ORGANIZATION if p.is_read else None


def _hr_everything(p: Permission) -> Scope | None:
    """HR Manager: every HR permission that applies beyond the person themself."""
    return Scope.ORGANIZATION if p.module == "hr" and Scope.ORGANIZATION in p.scopes else None


ORG, SELF = Scope.ORGANIZATION, Scope.SELF


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    key: str
    name: str
    description: str
    requires_mfa: bool = False
    protected: bool = False  # can't be edited or deleted (Super Administrator only)
    rule: GrantRule | None = None
    grants: dict[str, Scope] = field(default_factory=dict)

    def resolve(self) -> dict[str, Scope]:
        """The template's grants against the current catalogue."""
        result = dict(self.grants)
        if self.rule is not None:
            for permission in catalogue.permissions().values():
                scope = self.rule(permission)
                if scope is not None:
                    result[permission.key] = scope
        return dict(sorted(result.items()))


SUPER_ADMIN = "super_administrator"
ORG_ADMIN = "organization_administrator"

TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        SUPER_ADMIN,
        "Super Administrator",
        "Everything, including technical settings. Can't be edited or removed.",
        requires_mfa=True,
        protected=True,
        rule=_everything,
    ),
    RoleTemplate(
        ORG_ADMIN,
        "Organisation Administrator",
        "Everything except technical settings such as mail, branding and security.",
        requires_mfa=True,
        rule=_all_but_system,
    ),
    RoleTemplate(
        "auditor",
        "Auditor",
        "Reads everything, including the audit log. Changes nothing.",
        rule=_read_everything,
    ),
    RoleTemplate(
        "employee",
        "Employee",
        "Self-service: their own profile, requests and payslips.",
        grants={
            "hr.employee.read": ORG,  # the colleague directory
            "hr.employee.personal.read": SELF,
            "hr.employee.personal.update": SELF,  # self-service fields only (ADR 0017)
            "hr.employee.statutory.read": SELF,
            "hr.employee.banking.read": SELF,
            "hr.employee.compensation.read": SELF,
            "hr.employee.sensitive.read": SELF,
            "hr.profile.request_change": SELF,
        },
    ),
    RoleTemplate(
        "line_manager",
        "Line Manager",
        "Approves their team's requests and sees their team.",
        grants={"hr.employee.personal.read": Scope.TEAM},  # (confirm)
    ),
    RoleTemplate(
        "hr_officer",
        "HR Officer",
        "Day-to-day HR records and requests.",
        grants={
            "hr.employee.read": ORG,
            "hr.employee.create": ORG,
            "hr.employee.update": ORG,
            "hr.employee.personal.read": ORG,
            "hr.employee.personal.update": ORG,
            "hr.employee.statutory.read": ORG,
            "hr.employee.statutory.update": ORG,
            "hr.employee.banking.read": ORG,
            "hr.employee.banking.update": ORG,
            "hr.checklist.manage": ORG,
            "hr.exit.manage": ORG,
            "identity.user.invite": ORG,
            "org.structure.read": ORG,
        },
    ),
    RoleTemplate(
        "hr_manager",
        "HR Manager",
        "All HR records, including sensitive ones.",
        rule=_hr_everything,
        grants={"identity.user.invite": ORG, "org.structure.read": ORG},
    ),
    RoleTemplate(
        "it_support",
        "IT Support",
        "Completes the IT tasks on onboarding and offboarding checklists.",
        grants={"hr.employee.read": ORG, "hr.checklist.it": ORG},
    ),
    RoleTemplate("payroll_officer", "Payroll Officer", "Prepares payroll runs.", requires_mfa=True),
    RoleTemplate("finance_officer", "Finance Officer", "Day-to-day accounting.", requires_mfa=True),
    RoleTemplate(
        "finance_manager",
        "Finance Manager",
        "Approves and posts finance documents.",
        requires_mfa=True,
    ),
    RoleTemplate("expense_approver", "Expense Approver", "Approves expense claims."),
    RoleTemplate("reimburser", "Reimburser", "Pays out approved expense claims."),
    RoleTemplate("inventory_clerk", "Inventory Clerk", "Receives, issues and counts stock."),
    RoleTemplate(
        "procurement_officer", "Procurement Officer", "Requisitions, RFQs and purchase orders."
    ),
    RoleTemplate("support_agent", "Support Agent", "Works the support ticket queue."),
    RoleTemplate("fleet_manager", "Fleet Manager", "Vehicles, drivers, fuel and maintenance."),
)


def by_key(key: str) -> RoleTemplate:
    return next(t for t in TEMPLATES if t.key == key)
