"""Finance permissions (ADR 0023). The ledger is organisation-wide: every finance
permission is granted at organisation scope."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

ACCOUNT_READ = Permission(
    "finance.account.read",
    "View the chart of accounts",
    "See accounts and their balances.",
    scopes=ORG_ONLY,
)
ACCOUNT_MANAGE = Permission(
    "finance.account.manage",
    "Manage the chart of accounts",
    "Add accounts, rename them, and deactivate ones no longer used.",
    scopes=ORG_ONLY,
)
JOURNAL_READ = Permission(
    "finance.journal.read",
    "View journals",
    "See drafted, held and posted journals and their lines.",
    scopes=ORG_ONLY,
)
JOURNAL_CREATE = Permission(
    "finance.journal.create",
    "Draft journals",
    "Draft manual journals and set up recurring ones.",
    scopes=ORG_ONLY,
)
JOURNAL_POST = Permission(
    "finance.journal.post",
    "Post journals",
    "Post drafted and held journals, and reverse posted ones.",
    scopes=ORG_ONLY,
)
PERIOD_MANAGE = Permission(
    "finance.period.manage",
    "Manage financial periods",
    "Open financial years, close and reopen months, and close the year.",
    scopes=ORG_ONLY,
)
REPORT_READ = Permission(
    "finance.report.read",
    "View financial reports",
    "Trial balance, profit and loss, balance sheet, cash flow and the general ledger.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    ACCOUNT_READ,
    ACCOUNT_MANAGE,
    JOURNAL_READ,
    JOURNAL_CREATE,
    JOURNAL_POST,
    PERIOD_MANAGE,
    REPORT_READ,
)

SOD_RULES = (
    SodRule(
        key="finance.journal_four_eyes",
        permission=JOURNAL_POST.key,
        conflict="creator_is_actor",
        message="Someone else posts a journal you drafted.",
    ),
)
