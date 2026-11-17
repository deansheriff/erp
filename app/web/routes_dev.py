"""Development-only previews of the shell and shared components.

Mounted only when KX_ENV is development or test. They render sample data so the
Stitch designs can be reviewed in the real templates before any module exists.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.authz.declarations import public, screen
from app.web.navigation import (
    MODULES,
    Breadcrumb,
    NavModule,
    NavSection,
    ShellContext,
    ShellUser,
)
from app.web.templating import templates

router = APIRouter(prefix="/_dev", include_in_schema=False)


@dataclass(frozen=True, slots=True)
class SampleClaim:
    number: str
    employee: str
    department: str
    submitted: date
    amount: Decimal
    status: str
    status_label: str


SAMPLE_CLAIMS = (
    SampleClaim(
        "EXP-2026-000142",
        "Adaeze Okafor",
        "Executive",
        date(2026, 11, 17),
        Decimal("45000.00"),
        "info",
        "Submitted",
    ),
    SampleClaim(
        "EXP-2026-000139",
        "Babatunde Adeyemi",
        "Operations",
        date(2026, 11, 16),
        Decimal("128750.50"),
        "success",
        "Approved",
    ),
    SampleClaim(
        "EXP-2026-000135",
        "Fatima Bello",
        "Finance",
        date(2026, 11, 15),
        Decimal("9200.00"),
        "warning",
        "Pending",
    ),
    SampleClaim(
        "EXP-2026-000128",
        "Chinedu Eze",
        "Engineering",
        date(2026, 11, 14),
        Decimal("-3500.00"),
        "danger",
        "Rejected",
    ),
    SampleClaim(
        "EXP-2026-000121",
        "Aisha Mohammed",
        "Sales",
        date(2026, 11, 12),
        Decimal("62300.00"),
        "neutral",
        "Draft",
    ),
)


def _sample_shell(
    active_module: str, active_section: str | None, crumbs: list[str]
) -> ShellContext:
    expenses_sections = (
        NavSection("My claims", "#"),
        NavSection("Approvals", "#", count=4),
        NavSection("All claims", "/_dev/shell"),
        NavSection("Cash advances", "#"),
        NavSection("Categories", "#"),
        NavSection("Reports", "#"),
    )
    modules = tuple(
        NavModule(
            m.key,
            m.label,
            m.icon,
            "#" if m.key != "expenses" else "/_dev/shell",
            expenses_sections if m.key == "expenses" else (),
        )
        for m in MODULES
    )
    return ShellContext(
        org_name="Adeyemi Holdings Ltd",
        user=ShellUser("Fatima Bello", "Finance Manager"),
        modules=modules,
        active_module=active_module,
        active_section=active_section,
        breadcrumbs=tuple(Breadcrumb(c, "#") for c in crumbs),
        approvals_count=7,
        has_unread=True,
    )


@router.get("/shell", response_class=HTMLResponse)
@public
@screen("core.shell")
def shell_preview(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dev/shell_preview.html",
        {
            "shell": _sample_shell("expenses", "All claims", ["Expenses", "All claims"]),
            "claims": SAMPLE_CLAIMS,
            "page_title": "All claims",
        },
    )


@router.get("/components", response_class=HTMLResponse)
@public
@screen("core.components")
def components_preview(request: Request) -> HTMLResponse:
    steps = [
        {"label": "Draft", "meta": "Chinedu Eze, 14 Nov", "state": "done"},
        {"label": "Submitted", "meta": "15 Nov", "state": "done"},
        {"label": "Manager approval", "meta": "Adaeze Okafor", "state": "current"},
        {"label": "Finance approval", "meta": "Pending", "state": "todo"},
        {"label": "Reimbursed", "meta": "Final stage", "state": "todo"},
    ]
    return templates.TemplateResponse(
        request,
        "dev/components.html",
        {
            "shell": _sample_shell("home", None, ["Components"]),
            "claims": SAMPLE_CLAIMS[:4],
            "steps": steps,
            "page_title": "Components",
        },
    )


@router.get("/errors/{kind}", response_class=HTMLResponse)
@public
@screen("core.system_pages")
def error_preview(request: Request, kind: str) -> HTMLResponse:
    template = {
        "403": "errors/no_access.html",
        "404": "errors/not_found.html",
    }.get(kind, "errors/server_error.html")
    return templates.TemplateResponse(
        request,
        template,
        {
            "message": "Something went wrong on our side. Please try again in a moment.",
            "request_id": "6f1c2e9a-4b7d-4e21-9c3f-0a8d5b2e7c14",
            "resource_label": "Payroll runs",
        },
    )
