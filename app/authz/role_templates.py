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
    """HR Manager: every HR, leave and attendance permission that applies beyond the person
    themself."""
    in_scope = p.module in {"hr", "leave", "attendance"} and Scope.ORGANIZATION in p.scopes
    return Scope.ORGANIZATION if in_scope else None


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
            "leave.request.create": SELF,
            "leave.request.read": SELF,
            "leave.balance.read": SELF,
            "attendance.clock.use": SELF,
            "attendance.record.read": SELF,
            "attendance.correction.request": SELF,
            "attendance.photo.view": SELF,
            "expenses.claim.create": SELF,
            "expenses.claim.read": SELF,
            "payroll.pay.read": SELF,
            "payroll.payslip.read": SELF,  # once the run is paid
            "payroll.relief.declare": SELF,
            "payroll.loan.read": SELF,
            "payroll.certificate.read": SELF,  # once December is paid
            "inventory.request.create": SELF,
            "inventory.request.read": SELF,
            "procurement.requisition.create": SELF,
            "procurement.requisition.read": SELF,
            "projects.time.log": SELF,
            "support.ticket.create": SELF,
            "support.ticket.read": SELF,
            "fleet.vehicle.use": SELF,
            "chat.conversation.use": SELF,
            "assistant.policy.ask": SELF,
        },
    ),
    RoleTemplate(
        "line_manager",
        "Line Manager",
        "Approves their team's requests and sees their team.",
        grants={
            "hr.employee.personal.read": Scope.TEAM,  # (confirm)
            "leave.request.read": Scope.TEAM,
            "leave.request.approve": Scope.TEAM,
            "leave.balance.read": Scope.TEAM,
            "attendance.record.read": Scope.TEAM,
            "attendance.record.approve": Scope.TEAM,
            "expenses.claim.read": Scope.TEAM,
            "expenses.claim.approve": Scope.TEAM,
            "inventory.request.read": Scope.TEAM,
            "inventory.request.approve": Scope.TEAM,
            "procurement.requisition.read": Scope.TEAM,
            "procurement.requisition.approve": Scope.TEAM,
            "projects.time.approve": ORG,  # their reports' time on projects they manage
            "assistant.evidence.summarise": Scope.TEAM,
        },
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
            "leave.request.create": ORG,  # recording leave for someone
            "leave.request.read": ORG,
            "leave.request.approve_hr": ORG,
            "leave.balance.read": ORG,
            "attendance.record.read": ORG,
            "attendance.record.approve": ORG,
            "attendance.correction.request": ORG,  # recording a correction for someone
            "attendance.photo.view": ORG,
            "attendance.schedule.manage": ORG,
            "payroll.relief.verify": ORG,
            "assistant.policy.manage": ORG,
            "assistant.hiring.draft": ORG,
        },
    ),
    RoleTemplate(
        "hr_manager",
        "HR Manager",
        "All HR records, including sensitive ones.",
        rule=_hr_everything,
        grants={
            "identity.user.invite": ORG,
            "org.structure.read": ORG,
            "assistant.policy.manage": ORG,
            "assistant.hiring.draft": ORG,
            "assistant.evidence.summarise": ORG,
        },
    ),
    RoleTemplate(
        "it_support",
        "IT Support",
        "Completes the IT tasks on onboarding and offboarding checklists.",
        grants={"hr.employee.read": ORG, "hr.checklist.it": ORG},
    ),
    RoleTemplate(
        "payroll_officer",
        "Payroll Officer",
        "Prepares payroll runs.",
        requires_mfa=True,
        grants={
            "payroll.pay.read": ORG,
            "payroll.pay.manage": ORG,
            "payroll.structure.manage": ORG,
            "payroll.run.read": ORG,
            "payroll.run.prepare": ORG,
            "payroll.run.review": ORG,  # another officer reviews what one calculated
            "payroll.payslip.read": ORG,
            "payroll.relief.verify": ORG,
            "payroll.remittance.export": ORG,
            "payroll.loan.read": ORG,
            "payroll.loan.manage": ORG,
            "payroll.certificate.read": ORG,
        },
    ),
    RoleTemplate(
        "finance_officer",
        "Finance Officer",
        "Day-to-day accounting.",
        requires_mfa=True,
        grants={
            "expenses.claim.read": ORG,
            "payroll.run.read": ORG,
            "payroll.run.pay": ORG,
            "payroll.remittance.export": ORG,  # uploads the bank file
            "payroll.loan.read": ORG,
            "finance.account.read": ORG,
            "finance.journal.read": ORG,
            "finance.journal.create": ORG,
            "finance.report.read": ORG,
            "finance.customer.read": ORG,
            "finance.customer.manage": ORG,
            "finance.sales.read": ORG,
            "finance.sales.create": ORG,
            "finance.receipt.create": ORG,
            "finance.tax.read": ORG,
            "finance.supplier.read": ORG,
            "finance.supplier.manage": ORG,
            "finance.purchase.read": ORG,
            "finance.purchase.create": ORG,
            "finance.payment.create": ORG,
            "finance.bank.read": ORG,
            "finance.bank.reconcile": ORG,
            "finance.asset.read": ORG,
            "finance.asset.manage": ORG,
            "inventory.item.read": ORG,
        },
    ),
    RoleTemplate(
        "finance_manager",
        "Finance Manager",
        "Approves and posts finance documents.",
        requires_mfa=True,
        grants={
            "expenses.claim.read": ORG,
            "expenses.claim.approve_any": ORG,  # steps in when a manager can't
            "expenses.claim.approve_finance": ORG,
            "expenses.settings.manage": ORG,
            "payroll.run.read": ORG,
            "payroll.run.review": ORG,
            "payroll.run.approve": ORG,
            "payroll.run.post": ORG,
            "payroll.settings.manage": ORG,
            "payroll.remittance.export": ORG,
            "payroll.loan.read": ORG,
            "payroll.loan.approve": ORG,
            "payroll.certificate.read": ORG,
            "finance.account.read": ORG,
            "finance.account.manage": ORG,
            "finance.journal.read": ORG,
            "finance.journal.create": ORG,
            "finance.journal.post": ORG,
            "finance.period.manage": ORG,
            "finance.report.read": ORG,
            "finance.customer.read": ORG,
            "finance.customer.manage": ORG,
            "finance.sales.read": ORG,
            "finance.sales.create": ORG,
            "finance.credit_note.issue": ORG,  # a credit note someone else drafted
            "finance.receipt.create": ORG,
            "finance.receipt.reverse": ORG,
            "finance.tax.read": ORG,
            "finance.tax.manage": ORG,
            "finance.supplier.read": ORG,
            "finance.supplier.manage": ORG,
            "finance.purchase.read": ORG,
            "finance.purchase.create": ORG,
            "finance.purchase.approve": ORG,  # what someone else drafted
            "finance.bill.override": ORG,
            "finance.payment.create": ORG,
            "finance.payment.approve": ORG,  # what someone else prepared
            "finance.bank.read": ORG,
            "finance.bank.manage": ORG,
            "finance.bank.reconcile": ORG,
            "finance.bank.approve": ORG,  # what someone else prepared
            "finance.asset.read": ORG,
            "finance.asset.manage": ORG,
            "finance.asset.approve": ORG,  # what someone else prepared
            "inventory.item.read": ORG,
            "procurement.requisition.read": ORG,
            "procurement.requisition.approve_finance": ORG,
            "procurement.rfq.read": ORG,
            "procurement.settings.manage": ORG,
            "projects.project.read": ORG,
            "projects.rate.manage": ORG,
            "projects.settings.manage": ORG,
            "fleet.cost.read": ORG,
        },
    ),
    RoleTemplate(
        "expense_approver",
        "Expense Approver",
        "Approves expense claims.",
        grants={"expenses.claim.read": Scope.TEAM, "expenses.claim.approve": Scope.TEAM},
    ),
    RoleTemplate(
        "reimburser",
        "Reimburser",
        "Pays out approved expense claims.",
        grants={"expenses.claim.read": ORG, "expenses.claim.reimburse": ORG},
    ),
    RoleTemplate(
        "inventory_clerk",
        "Inventory Clerk",
        "Receives, issues and counts stock.",
        grants={
            "inventory.item.read": ORG,
            "inventory.stock.move": ORG,
            "inventory.request.read": ORG,
        },
    ),
    RoleTemplate(
        "inventory_manager",
        "Inventory Manager",
        "Keeps items and warehouses, and approves stock adjustments and counts.",
        grants={
            "inventory.item.read": ORG,
            "inventory.item.manage": ORG,
            "inventory.stock.move": ORG,
            "inventory.stock.approve": ORG,  # what someone else recorded
            "inventory.request.read": ORG,
            "inventory.request.approve_any": ORG,
        },
    ),
    RoleTemplate(
        "procurement_officer",
        "Procurement Officer",
        "Requisitions, RFQs and purchase orders.",
        grants={
            "procurement.requisition.read": ORG,
            "procurement.rfq.read": ORG,
            "procurement.rfq.manage": ORG,
            "procurement.supplier.rate": ORG,
            "finance.supplier.read": ORG,
            "finance.purchase.read": ORG,
            "finance.purchase.create": ORG,  # drafts; someone else approves (ADR 0030)
            "inventory.item.read": ORG,
        },
    ),
    RoleTemplate(
        "director",
        "Director",
        "Approves the largest requisitions and steps in when there's no line manager.",
        grants={
            "procurement.requisition.read": ORG,
            "procurement.requisition.approve_director": ORG,
            "procurement.requisition.approve_any": ORG,
            "procurement.rfq.read": ORG,
            "procurement.settings.manage": ORG,
            "projects.project.read": ORG,
            "projects.time.approve_any": ORG,
            "projects.settings.manage": ORG,
            "fleet.cost.read": ORG,
        },
    ),
    RoleTemplate(
        "project_manager",
        "Project Manager",
        "Sets up projects, manages the ones they lead and approves their team's time.",
        grants={
            "projects.project.create": ORG,
            "projects.time.approve": ORG,  # on projects they manage
            "finance.customer.read": ORG,
        },
    ),
    RoleTemplate(
        "support_agent",
        "Support Agent",
        "Works the support ticket queue.",
        grants={
            "support.ticket.read": ORG,  # their teams' tickets, as the service filters
            "support.ticket.work": ORG,
            "support.report.read": ORG,
            "finance.customer.read": ORG,  # to record customers' tickets
            "assistant.ticket.suggest": ORG,
        },
    ),
    RoleTemplate(
        "fleet_manager",
        "Fleet Manager",
        "Vehicles, drivers, fuel and maintenance.",
        grants={
            "fleet.vehicle.read": ORG,
            "fleet.vehicle.manage": ORG,
            "fleet.cost.read": ORG,
            "fleet.vehicle.use": ORG,
        },
    ),
)


def by_key(key: str) -> RoleTemplate:
    return next(t for t in TEMPLATES if t.key == key)
