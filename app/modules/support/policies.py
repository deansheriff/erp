"""Support permissions (ADR 0032). Employees see their own tickets; agents work their
teams' queues and customers' tickets; team leads assign within their team."""

from app.authz.permissions import ORG_ONLY, Permission

TICKET_CREATE = Permission(
    "support.ticket.create",
    "Raise support tickets",
    "Ask for help from a support team, and follow your own tickets.",
)
TICKET_READ = Permission(
    "support.ticket.read",
    "View support tickets",
    "See support tickets within scope, with their public replies.",
)
TICKET_WORK = Permission(
    "support.ticket.work",
    "Work support tickets",
    "Take and work tickets in your teams' queues, record customers' tickets, reply, add "
    "internal notes and resolve. Team leads also assign.",
    scopes=ORG_ONLY,
)
REPORT_READ = Permission(
    "support.report.read",
    "View the SLA report",
    "See response and resolution targets met and breached, and ratings, by team and agent.",
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "support.settings.manage",
    "Manage support settings",
    "Set up teams, categories, SLA targets, business hours and when resolved tickets close.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (TICKET_CREATE, TICKET_READ, TICKET_WORK, REPORT_READ, SETTINGS_MANAGE)
SOD_RULES = ()
