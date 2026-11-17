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


@dataclass(frozen=True, slots=True)
class NavModule:
    key: str
    label: str
    icon: str  # Lucide icon id
    href: str
    sections: tuple[NavSection, ...] = field(default_factory=tuple)


MODULES: tuple[NavModule, ...] = (
    NavModule("home", "Home", "house", "/"),
    NavModule("hr", "HR", "id-card", "/hr"),
    NavModule("leave", "Leave", "calendar-check", "/leave"),
    NavModule("attendance", "Attendance", "clock", "/attendance"),
    NavModule("expenses", "Expenses", "receipt", "/expenses"),
    NavModule("payroll", "Payroll", "banknote", "/payroll"),
    NavModule("finance", "Finance", "chart-line", "/finance"),
    NavModule("inventory", "Inventory", "package", "/inventory"),
    NavModule("procurement", "Procurement", "shopping-cart", "/procurement"),
    NavModule("projects", "Projects", "briefcase", "/projects"),
    NavModule("support", "Support", "headset", "/support"),
    NavModule("fleet", "Fleet", "truck", "/fleet"),
    NavModule("collaboration", "Chat", "messages-square", "/chat"),
    NavModule("ai", "Assistant", "sparkles", "/assistant"),
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

    @property
    def org_initials(self) -> str:
        words = [w for w in self.org_name.split() if w[:1].isalpha()]
        return "".join(w[0] for w in words[:2]).upper() or "KX"
