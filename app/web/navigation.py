"""The module list shown in the sidebar and the mobile "More" sheet.

Each module's hue key matches a `.module-<key>` class in tokens.css. From Phase 3 the
shell shows only modules the viewer holds at least one permission for.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class NavSection:
    label: str
    href: str
    count: int | None = None
    # Shown only to holders (of any one, when several are given).
    permission: str | tuple[str, ...] | None = None
    # Only to those who hold it beyond their own records (self-service lives elsewhere).
    wide: bool = False


@dataclass(frozen=True, slots=True)
class NavModule:
    key: str
    label: str
    icon: str  # Lucide icon id
    href: str
    sections: tuple[NavSection, ...] = field(default_factory=tuple)


MODULES: tuple[NavModule, ...] = (
    NavModule("home", "Home", "house", "/"),
    NavModule(
        "hr",
        "HR",
        "id-card",
        "/hr/org-chart",
        (
            NavSection("Directory", "/directory", permission="hr.employee.read"),
            NavSection("Org chart", "/hr/org-chart", permission="org.structure.read"),
            NavSection("People", "/hr/people", permission="hr.employee.update"),
            NavSection(
                "Change requests",
                "/hr/change-requests",
                permission=("hr.employee.statutory.update", "hr.employee.banking.update"),
            ),
            NavSection(
                "Joiners & leavers",
                "/hr/checklists",
                permission=("hr.checklist.manage", "hr.checklist.it"),
            ),
            NavSection("Departments", "/hr/departments", permission="org.structure.read"),
            NavSection("Structure setup", "/hr/structure", permission="org.structure.read"),
            NavSection("HR settings", "/hr/settings", permission="hr.settings.manage"),
        ),
    ),
    NavModule(
        "leave",
        "Leave",
        "calendar-check",
        "/leave",
        (
            NavSection("My leave", "/leave", permission="leave.request.read"),
            NavSection(
                "Approvals",
                "/leave/approvals",
                permission=("leave.request.approve", "leave.request.approve_hr"),
            ),
            NavSection("Team calendar", "/leave/calendar", permission="leave.request.read"),
            NavSection("Balances", "/leave/balances", permission="leave.balance.read"),
            NavSection("Settings", "/leave/settings", permission="leave.settings.manage"),
        ),
    ),
    NavModule(
        "attendance",
        "Attendance",
        "clock",
        "/attendance",
        (
            NavSection("Clock in", "/attendance", permission="attendance.clock.use"),
            NavSection("Timesheet", "/attendance/timesheet", permission="attendance.record.read"),
            NavSection(
                "Team today",
                "/attendance/team",
                permission="attendance.record.approve",
            ),
            NavSection(
                "Monthly summary", "/attendance/summary", permission="attendance.record.read"
            ),
            NavSection(
                "Shifts & roster", "/attendance/roster", permission="attendance.schedule.manage"
            ),
            NavSection("Settings", "/attendance/settings", permission="attendance.settings.manage"),
        ),
    ),
    NavModule(
        "expenses",
        "Expenses",
        "receipt",
        "/expenses",
        (
            NavSection("My expenses", "/expenses", permission="expenses.claim.create"),
            NavSection(
                "Approvals",
                "/expenses/approvals",
                permission=(
                    "expenses.claim.approve",
                    "expenses.claim.approve_any",
                    "expenses.claim.approve_finance",
                ),
            ),
            NavSection("Payments", "/expenses/payments", permission="expenses.claim.reimburse"),
            NavSection("Settings", "/expenses/settings", permission="expenses.settings.manage"),
        ),
    ),
    NavModule(
        "payroll",
        "Payroll",
        "banknote",
        "/payroll",
        (
            NavSection("My payslips", "/payslips", permission="payroll.payslip.read"),
            NavSection("Runs", "/payroll", permission="payroll.run.read"),
            NavSection("People's pay", "/payroll/people", permission="payroll.pay.manage"),
            NavSection(
                "Salary structures", "/payroll/structures", permission="payroll.structure.manage"
            ),
            NavSection("Remittances", "/payroll/remittances", permission="payroll.run.read"),
            NavSection("Loans", "/payroll/loans", permission="payroll.loan.read", wide=True),
            NavSection(
                "Tax certificates",
                "/payroll/certificates",
                permission="payroll.certificate.read",
                wide=True,
            ),
            NavSection("Rent relief", "/payroll/reliefs", permission="payroll.relief.verify"),
            NavSection("Settings", "/payroll/settings", permission="payroll.settings.manage"),
        ),
    ),
    NavModule(
        "finance",
        "Finance",
        "chart-line",
        "/finance/journals",
        (
            NavSection("Customers", "/finance/customers", permission="finance.customer.read"),
            NavSection("Sales", "/finance/sales", permission="finance.sales.read"),
            NavSection("Aging", "/finance/aging", permission="finance.customer.read"),
            NavSection("Suppliers", "/finance/suppliers", permission="finance.supplier.read"),
            NavSection("Purchases", "/finance/purchases", permission="finance.purchase.read"),
            NavSection(
                "Payables aging", "/finance/payables-aging", permission="finance.supplier.read"
            ),
            NavSection("Banking", "/finance/banking", permission="finance.bank.read"),
            NavSection("Transfers", "/finance/transfers", permission="finance.bank.read"),
            NavSection("Fixed assets", "/finance/assets", permission="finance.asset.read"),
            NavSection("Journals", "/finance/journals", permission="finance.journal.read"),
            NavSection("Chart of accounts", "/finance/accounts", permission="finance.account.read"),
            NavSection("Recurring", "/finance/recurring", permission="finance.journal.read"),
            NavSection("Periods", "/finance/periods", permission="finance.journal.read"),
            NavSection("Reports", "/finance/reports", permission="finance.report.read"),
            NavSection("Tax returns", "/finance/tax/returns", permission="finance.tax.read"),
            NavSection("Tax settings", "/finance/tax", permission="finance.tax.read"),
        ),
    ),
    NavModule(
        "inventory",
        "Inventory",
        "package",
        "/inventory/items",
        (
            NavSection("Items", "/inventory/items", permission="inventory.item.read"),
            NavSection("Movements", "/inventory/movements", permission="inventory.item.read"),
            NavSection("Counts", "/inventory/counts", permission="inventory.item.read"),
            NavSection(
                "Material requests", "/inventory/requests", permission="inventory.request.read"
            ),
            NavSection("Setup", "/inventory/setup", permission="inventory.item.read"),
        ),
    ),
    NavModule(
        "procurement",
        "Procurement",
        "shopping-cart",
        "/procurement/requisitions",
        (
            NavSection(
                "Requisitions",
                "/procurement/requisitions",
                permission="procurement.requisition.read",
            ),
            NavSection("RFQs", "/procurement/rfqs", permission="procurement.rfq.read"),
            NavSection(
                "Supplier performance", "/procurement/suppliers", permission="procurement.rfq.read"
            ),
            NavSection(
                "Settings", "/procurement/settings", permission="procurement.settings.manage"
            ),
        ),
    ),
    NavModule(
        "projects",
        "Projects",
        "briefcase",
        "/projects",
        (
            NavSection(
                "Projects",
                "/projects",
                permission=("projects.time.log", "projects.project.read"),
            ),
            NavSection("My timesheet", "/projects/timesheet", permission="projects.time.log"),
            NavSection("Time approvals", "/projects/approvals", permission="projects.time.approve"),
            NavSection(
                "Profitability", "/projects/profitability", permission="projects.project.read"
            ),
            NavSection(
                "Settings",
                "/projects/settings",
                permission=("projects.rate.manage", "projects.settings.manage"),
            ),
        ),
    ),
    NavModule(
        "support",
        "Support",
        "headset",
        "/support",
        (
            NavSection(
                "Tickets", "/support", permission=("support.ticket.create", "support.ticket.work")
            ),
            NavSection("SLA report", "/support/report", permission="support.report.read"),
            NavSection("Settings", "/support/settings", permission="support.settings.manage"),
        ),
    ),
    NavModule(
        "fleet",
        "Fleet",
        "truck",
        "/fleet",
        (
            NavSection("My fleet", "/fleet", permission="fleet.vehicle.use"),
            NavSection("Vehicles", "/fleet/vehicles", permission="fleet.vehicle.read"),
            NavSection("Bookings", "/fleet/bookings", permission="fleet.vehicle.manage"),
            NavSection("Incidents", "/fleet/incidents", permission="fleet.vehicle.use"),
            NavSection("Drivers", "/fleet/drivers", permission="fleet.vehicle.read"),
            NavSection("Cost per vehicle", "/fleet/costs", permission="fleet.cost.read"),
            NavSection("Settings", "/fleet/settings", permission="fleet.vehicle.manage"),
        ),
    ),
    NavModule(
        "chat",
        "Chat",
        "messages-square",
        "/chat",
        (
            NavSection("Chat", "/chat", permission="chat.conversation.use"),
            NavSection("New conversation", "/chat/new", permission="chat.conversation.use"),
            NavSection("Settings", "/chat/settings", permission="chat.settings.manage"),
        ),
    ),
    NavModule(
        "assistant",
        "Assistant",
        "sparkles",
        "/assistant",
        (
            NavSection("Ask about policies", "/assistant", permission="assistant.policy.ask"),
            NavSection(
                "Job descriptions", "/assistant/hiring", permission="assistant.hiring.draft"
            ),
            NavSection(
                "Appraisal evidence",
                "/assistant/evidence",
                permission="assistant.evidence.summarise",
            ),
            NavSection("Policy library", "/assistant/policies", permission="assistant.policy.ask"),
            NavSection("History", "/assistant/history", permission="assistant.policy.ask"),
            NavSection("Settings", "/assistant/settings", permission="system.ai.manage"),
        ),
    ),
)

ADMIN_MODULE = NavModule(
    "admin",
    "Administration",
    "shield-check",
    "/admin/users",
    (
        NavSection("Users", "/admin/users", permission="identity.user.read"),
        NavSection("Roles", "/admin/roles", permission="authz.role.read"),
        NavSection("Delegations", "/admin/delegations", permission="authz.delegation.manage"),
        NavSection("Audit log", "/admin/audit", permission="audit.log.read"),
        NavSection("Files", "/admin/files", permission="files.file.manage"),
        NavSection("Organisation", "/admin/organisation", permission="system.branding.manage"),
        NavSection("Mail settings", "/admin/mail", permission="system.mail.manage"),
    ),
)


@dataclass(frozen=True, slots=True)
class Breadcrumb:
    label: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class ShellUser:
    name: str
    role: str

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper()


@dataclass(frozen=True, slots=True)
class ShellContext:
    """Everything the layout needs. Built by each web route (Phase 2: from the session)."""

    org_name: str
    user: ShellUser
    modules: tuple[NavModule, ...]
    active_module: str = "home"
    active_section: str | None = None
    breadcrumbs: tuple[Breadcrumb, ...] = ()
    approvals_count: int = 0
    has_unread: bool = False
    csrf_token: str | None = None
    org_logo_url: str | None = None

    @property
    def org_initials(self) -> str:
        words = [w for w in self.org_name.split() if w[:1].isalpha()]
        return "".join(w[0] for w in words[:2]).upper() or "KX"
