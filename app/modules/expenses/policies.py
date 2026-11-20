"""Expense permissions (ADR 0020)."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

CLAIM_CREATE = Permission(
    "expenses.claim.create",
    "Make expense claims",
    "Write and submit claims and cash advance requests.",
)
CLAIM_READ = Permission(
    "expenses.claim.read",
    "View expense claims",
    "See claims, advances and their receipts within scope.",
)
CLAIM_APPROVE = Permission(
    "expenses.claim.approve",
    "Approve expense claims",
    "Decide the approval steps you are named on (as a manager, or by delegation).",
)
CLAIM_APPROVE_ANY = Permission(
    "expenses.claim.approve_any",
    "Step in on expense approvals",
    "Decide any manager step, e.g. when the manager is away or there is none.",
    scopes=ORG_ONLY,
)
CLAIM_APPROVE_FINANCE = Permission(
    "expenses.claim.approve_finance",
    "Give Finance approval for expenses",
    "Decide the Finance step of claims and advances above the Finance amount.",
    scopes=ORG_ONLY,
)
CLAIM_REIMBURSE = Permission(
    "expenses.claim.reimburse",
    "Pay expense claims",
    "Mark approved claims and advances paid, and record advance repayments.",
    scopes=ORG_ONLY,
)
CLAIM_REIMBURSE_OVERRIDE = Permission(
    "expenses.claim.reimburse_override",
    "Pay claims you approved",
    "Allows paying a claim or advance you approved yourself.",
    sensitive=True,
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "expenses.settings.manage",
    "Manage expense settings",
    "Categories, approval amounts, spending limits and ledger accounts.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    CLAIM_CREATE,
    CLAIM_READ,
    CLAIM_APPROVE,
    CLAIM_APPROVE_ANY,
    CLAIM_APPROVE_FINANCE,
    CLAIM_REIMBURSE,
    CLAIM_REIMBURSE_OVERRIDE,
    SETTINGS_MANAGE,
)

SOD_RULES = tuple(
    SodRule(
        key=f"expenses.no_self_{p.key.rsplit('.', 1)[1]}",
        permission=p.key,
        conflict="owner_is_actor",
        message="You can't approve your own claim."
        if "approve" in p.key
        else "You can't pay your own claim.",
    )
    for p in (CLAIM_APPROVE, CLAIM_APPROVE_ANY, CLAIM_APPROVE_FINANCE, CLAIM_REIMBURSE)
)
