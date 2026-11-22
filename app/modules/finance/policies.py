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

# Receivables and tax (ADR 0024)
CUSTOMER_READ = Permission(
    "finance.customer.read",
    "View customers",
    "See customers, their balances, aging and statements.",
    scopes=ORG_ONLY,
)
CUSTOMER_MANAGE = Permission(
    "finance.customer.manage",
    "Manage customers",
    "Add and change customers, and email them payment reminders.",
    scopes=ORG_ONLY,
)
SALES_READ = Permission(
    "finance.sales.read",
    "View sales documents",
    "See quotes, sales orders, invoices, credit notes and receipts.",
    scopes=ORG_ONLY,
)
SALES_CREATE = Permission(
    "finance.sales.create",
    "Raise sales documents",
    "Draft and issue quotes, sales orders and invoices, and draft credit notes.",
    scopes=ORG_ONLY,
)
CREDIT_NOTE_ISSUE = Permission(
    "finance.credit_note.issue",
    "Issue credit notes",
    "Issue a credit note someone else drafted.",
    scopes=ORG_ONLY,
)
RECEIPT_CREATE = Permission(
    "finance.receipt.create",
    "Record receipts",
    "Record customer receipts, allocate credit to invoices, and record WHT credit notes.",
    scopes=ORG_ONLY,
)
RECEIPT_REVERSE = Permission(
    "finance.receipt.reverse",
    "Reverse receipts",
    "Reverse a recorded receipt, for example a bounced cheque.",
    scopes=ORG_ONLY,
)
TAX_READ = Permission(
    "finance.tax.read",
    "View tax returns",
    "See VAT, withholding tax and stamp duty returns and the tax settings.",
    scopes=ORG_ONLY,
)
TAX_MANAGE = Permission(
    "finance.tax.manage",
    "Manage tax",
    "Change tax rates, types and receivables settings, and file returns.",
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
    CUSTOMER_READ,
    CUSTOMER_MANAGE,
    SALES_READ,
    SALES_CREATE,
    CREDIT_NOTE_ISSUE,
    RECEIPT_CREATE,
    RECEIPT_REVERSE,
    TAX_READ,
    TAX_MANAGE,
)

SOD_RULES = (
    SodRule(
        key="finance.journal_four_eyes",
        permission=JOURNAL_POST.key,
        conflict="creator_is_actor",
        message="Someone else posts a journal you drafted.",
    ),
    SodRule(
        key="finance.credit_note_four_eyes",
        permission=CREDIT_NOTE_ISSUE.key,
        conflict="creator_is_actor",
        message="Someone else issues a credit note you drafted.",
    ),
)
