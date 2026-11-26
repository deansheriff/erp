"""Project permissions (ADR 0031). Members and managers see their own projects by
membership; these permissions cover creating projects, seeing all of them with their
figures, logging and approving time, cost rates and settings."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

PROJECT_CREATE = Permission(
    "projects.project.create",
    "Create projects",
    "Set up projects and manage the ones you lead: members, budget, milestones, tasks and status.",
    scopes=ORG_ONLY,
)
PROJECT_READ = Permission(
    "projects.project.read",
    "View all projects",
    "See every project with its revenue, costs, labour and profitability.",
    scopes=ORG_ONLY,
)
TIME_LOG = Permission(
    "projects.time.log",
    "Log project time",
    "Record hours on projects you're a member of and submit them weekly.",
)
TIME_APPROVE = Permission(
    "projects.time.approve",
    "Approve project time",
    "Decide timesheets on projects you manage, or your reports' time on their own projects.",
    scopes=ORG_ONLY,
)
TIME_APPROVE_ANY = Permission(
    "projects.time.approve_any",
    "Step in on project time",
    "Decide any timesheet when neither the project manager nor a line manager can.",
    scopes=ORG_ONLY,
)
RATE_MANAGE = Permission(
    "projects.rate.manage",
    "Manage hourly cost rates",
    "Set the hourly cost of each person used for project labour cost.",
    scopes=ORG_ONLY,
    sensitive=True,
)
SETTINGS_MANAGE = Permission(
    "projects.settings.manage",
    "Manage project settings",
    "Change the budget alert thresholds.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    PROJECT_CREATE,
    PROJECT_READ,
    TIME_LOG,
    TIME_APPROVE,
    TIME_APPROVE_ANY,
    RATE_MANAGE,
    SETTINGS_MANAGE,
)

SOD_RULES = tuple(
    SodRule(
        key=f"projects.no_self_{p.key.rsplit('.', 1)[1]}",
        permission=p.key,
        conflict="owner_is_actor",
        message="You can't approve your own time.",
    )
    for p in (TIME_APPROVE, TIME_APPROVE_ANY)
)
