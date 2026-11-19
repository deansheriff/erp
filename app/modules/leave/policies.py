"""Leave permissions (ADR 0018)."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

REQUEST_CREATE = Permission(
    "leave.request.create",
    "Request leave",
    "Ask for leave (for yourself, or for others in scope, e.g. HR recording it).",
)
REQUEST_READ = Permission(
    "leave.request.read",
    "View leave",
    "See leave requests and the team calendar within scope.",
)
REQUEST_APPROVE = Permission(
    "leave.request.approve",
    "Approve leave",
    "Decide the manager step of leave requests within scope.",
)
REQUEST_APPROVE_HR = Permission(
    "leave.request.approve_hr",
    "Give HR approval for leave",
    "Decide the HR step for leave types that need it, and requests without a manager.",
    scopes=ORG_ONLY,
)
REQUEST_CANCEL = Permission(
    "leave.request.cancel",
    "Cancel anyone's leave",
    "Cancel approved leave that has already started, restoring the days.",
    scopes=ORG_ONLY,
)
BALANCE_READ = Permission(
    "leave.balance.read", "View leave balances", "See leave balances within scope."
)
BALANCE_ADJUST = Permission(
    "leave.balance.adjust",
    "Adjust leave balances",
    "Add or remove days from someone's balance, with a reason.",
    sensitive=True,
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "leave.settings.manage",
    "Manage leave settings",
    "Leave types, entitlement rules and public holidays.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    REQUEST_CREATE,
    REQUEST_READ,
    REQUEST_APPROVE,
    REQUEST_APPROVE_HR,
    REQUEST_CANCEL,
    BALANCE_READ,
    BALANCE_ADJUST,
    SETTINGS_MANAGE,
)

SOD_RULES = (
    SodRule(
        key="leave.no_self_approval",
        permission=REQUEST_APPROVE.key,
        conflict="owner_is_actor",
        message="You can't approve your own leave.",
    ),
    SodRule(
        key="leave.no_self_hr_approval",
        permission=REQUEST_APPROVE_HR.key,
        conflict="owner_is_actor",
        message="You can't approve your own leave.",
    ),
    SodRule(
        key="leave.no_self_adjustment",
        permission=BALANCE_ADJUST.key,
        conflict="subject_is_actor",
        message="You can't adjust your own leave balance.",
    ),
)
