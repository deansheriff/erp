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

# Payables (ADR 0025)
SUPPLIER_READ = Permission(
    "finance.supplier.read",
    "View suppliers",
    "See suppliers, their bank details, what is owed to them and the payables aging.",
    scopes=ORG_ONLY,
)
SUPPLIER_MANAGE = Permission(
    "finance.supplier.manage",
    "Manage suppliers",
    "Add and change suppliers, including their bank details.",
    scopes=ORG_ONLY,
)
PURCHASE_READ = Permission(
    "finance.purchase.read",
    "View purchases",
    "See purchase orders, goods received, bills, debit notes and supplier payments.",
    scopes=ORG_ONLY,
)
PURCHASE_CREATE = Permission(
    "finance.purchase.create",
    "Raise purchases",
    "Draft purchase orders, bills and debit notes, and record goods received.",
    scopes=ORG_ONLY,
)
PURCHASE_APPROVE = Permission(
    "finance.purchase.approve",
    "Approve purchases",
    "Approve purchase orders and bills, and issue debit notes, that someone else drafted.",
    scopes=ORG_ONLY,
)
BILL_OVERRIDE = Permission(
    "finance.bill.override",
    "Post held bills",
    "Post a bill that failed the three-way match, with a reason.",
    scopes=ORG_ONLY,
)
PAYMENT_CREATE = Permission(
    "finance.payment.create",
    "Prepare supplier payments",
    "Prepare payments to suppliers for someone else to approve.",
    scopes=ORG_ONLY,
)
PAYMENT_APPROVE = Permission(
    "finance.payment.approve",
    "Approve supplier payments",
    "Approve and post supplier payments someone else prepared, download the bank file, "
    "and reverse payments.",
    scopes=ORG_ONLY,
)
# Banking (ADR 0026)
BANK_READ = Permission(
    "finance.bank.read",
    "View banking",
    "See bank accounts, imported statements, matching, reconciliations and transfers.",
    scopes=ORG_ONLY,
)
BANK_MANAGE = Permission(
    "finance.bank.manage",
    "Manage bank accounts",
    "Add bank accounts, set their statement layouts, and keep the matching rules.",
    scopes=ORG_ONLY,
)
BANK_RECONCILE = Permission(
    "finance.bank.reconcile",
    "Reconcile bank accounts",
    "Import statements, match and give entries to lines, prepare reconciliations, and "
    "record transfers between accounts.",
    scopes=ORG_ONLY,
)
BANK_APPROVE = Permission(
    "finance.bank.approve",
    "Approve reconciliations and transfers",
    "Approve a reconciliation or a transfer someone else prepared, posting its journals.",
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
    SUPPLIER_READ,
    SUPPLIER_MANAGE,
    PURCHASE_READ,
    PURCHASE_CREATE,
    PURCHASE_APPROVE,
    BILL_OVERRIDE,
    PAYMENT_CREATE,
    PAYMENT_APPROVE,
    BANK_READ,
    BANK_MANAGE,
    BANK_RECONCILE,
    BANK_APPROVE,
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
    SodRule(
        key="finance.purchase_four_eyes",
        permission=PURCHASE_APPROVE.key,
        conflict="creator_is_actor",
        message="Someone else approves a purchase order, bill or debit note you drafted.",
    ),
    SodRule(
        key="finance.payment_four_eyes",
        permission=PAYMENT_APPROVE.key,
        conflict="creator_is_actor",
        message="Someone else approves a supplier payment you prepared.",
    ),
    SodRule(
        key="finance.bank_four_eyes",
        permission=BANK_APPROVE.key,
        conflict="creator_is_actor",
        message="Someone else approves a reconciliation or transfer you prepared.",
    ),
)
