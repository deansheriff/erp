"""Finance (first gate): the chart of accounts, financial years and periods, journals,
recurring journals and finance settings (ADR 0023).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = "organization"
# Frozen copy of the Phase 10a catalogue additions: (key, sensitive, widest scope).
PERMISSIONS = [
    ("finance.account.read", False, ORG),
    ("finance.account.manage", False, ORG),
    ("finance.journal.read", False, ORG),
    ("finance.journal.create", False, ORG),
    ("finance.journal.post", False, ORG),
    ("finance.period.manage", False, ORG),
    ("finance.report.read", False, ORG),
]
TEMPLATE_GRANTS = {
    "organization_administrator": {key: ORG for key, _, _ in PERMISSIONS},
    "finance_officer": {
        "finance.account.read": ORG,
        "finance.journal.read": ORG,
        "finance.journal.create": ORG,
        "finance.report.read": ORG,
    },
    "finance_manager": {key: ORG for key, _, _ in PERMISSIONS},
    "auditor": {
        "finance.account.read": ORG,
        "finance.journal.read": ORG,
        "finance.report.read": ORG,
    },
}
# A Nigerian SME chart (confirm before go-live), using the codes payroll and expenses
# already queue. (code, name, type, parent code, header, cash-flow class)
CHART = [
    ("1", "Assets", "asset", None, True, None),
    ("10", "Cash and bank", "asset", "1", True, None),
    ("1010", "Bank: main account", "asset", "10", False, "cash"),
    ("1020", "Cash on hand", "asset", "10", False, "cash"),
    ("11", "Receivables and other current assets", "asset", "1", True, None),
    ("1100", "Trade receivables", "asset", "11", False, "operating"),
    ("1200", "Inventory", "asset", "11", False, "operating"),
    ("1300", "Prepayments", "asset", "11", False, "operating"),
    ("1310", "VAT receivable (input VAT)", "asset", "11", False, "operating"),
    ("1320", "Withholding tax receivable", "asset", "11", False, "operating"),
    ("1410", "Staff cash advances", "asset", "11", False, "operating"),
    ("1420", "Staff loans", "asset", "11", False, "operating"),
    ("15", "Property, plant and equipment", "asset", "1", True, None),
    ("1510", "Land and buildings", "asset", "15", False, "investing"),
    ("1520", "Plant and machinery", "asset", "15", False, "investing"),
    ("1530", "Motor vehicles", "asset", "15", False, "investing"),
    ("1540", "Furniture and fittings", "asset", "15", False, "investing"),
    ("1550", "Computer equipment", "asset", "15", False, "investing"),
    ("1590", "Accumulated depreciation", "asset", "15", False, "investing"),
    ("2", "Liabilities", "liability", None, True, None),
    ("20", "Payables and accruals", "liability", "2", True, None),
    ("2010", "Trade payables", "liability", "20", False, "operating"),
    ("2020", "Accrued expenses", "liability", "20", False, "operating"),
    ("21", "Payroll liabilities", "liability", "2", True, None),
    ("2110", "PAYE payable", "liability", "21", False, "operating"),
    ("2120", "Pension payable", "liability", "21", False, "operating"),
    ("2130", "NHF payable", "liability", "21", False, "operating"),
    ("2140", "NHIS payable", "liability", "21", False, "operating"),
    ("2150", "NSITF payable", "liability", "21", False, "operating"),
    ("2160", "Other deductions payable", "liability", "21", False, "operating"),
    ("2170", "Net salaries payable", "liability", "21", False, "operating"),
    ("22", "Taxes payable", "liability", "2", True, None),
    ("2200", "VAT payable (output VAT)", "liability", "22", False, "operating"),
    ("2210", "Withholding tax payable", "liability", "22", False, "operating"),
    ("2220", "Stamp duty payable", "liability", "22", False, "operating"),
    ("2300", "Company income tax payable", "liability", "22", False, "operating"),
    ("25", "Long-term liabilities", "liability", "2", True, None),
    ("2500", "Long-term loans", "liability", "25", False, "financing"),
    ("3", "Equity", "equity", None, True, None),
    ("3100", "Share capital", "equity", "3", False, "financing"),
    ("3200", "Retained earnings", "equity", "3", False, "financing"),
    ("4", "Income", "income", None, True, None),
    ("4100", "Sales revenue", "income", "4", False, None),
    ("4200", "Service revenue", "income", "4", False, None),
    ("4900", "Other income", "income", "4", False, None),
    ("5", "Cost of sales", "expense", None, True, None),
    ("5100", "Cost of goods sold", "expense", "5", False, None),
    ("6", "Operating expenses", "expense", None, True, None),
    ("60", "Staff costs", "expense", "6", True, None),
    ("6000", "Salaries and wages", "expense", "60", False, None),
    ("6010", "Employer pension", "expense", "60", False, None),
    ("6020", "Employer NSITF", "expense", "60", False, None),
    ("6030", "Staff welfare", "expense", "60", False, None),
    ("61", "Travel and expenses", "expense", "6", True, None),
    ("6100", "Travel", "expense", "61", False, None),
    ("6110", "Accommodation", "expense", "61", False, None),
    ("6120", "Meals", "expense", "61", False, None),
    ("6130", "Fuel", "expense", "61", False, None),
    ("62", "Administration", "expense", "6", True, None),
    ("6200", "Training", "expense", "62", False, None),
    ("6300", "Office supplies", "expense", "62", False, None),
    ("6400", "Rent", "expense", "62", False, None),
    ("6410", "Utilities", "expense", "62", False, None),
    ("6420", "Telephone and internet", "expense", "62", False, None),
    ("6430", "Repairs and maintenance", "expense", "62", False, None),
    ("6440", "Insurance", "expense", "62", False, None),
    ("6450", "Professional fees", "expense", "62", False, None),
    ("6460", "Bank charges", "expense", "62", False, None),
    ("6470", "Advertising", "expense", "62", False, None),
    ("6500", "Depreciation", "expense", "62", False, None),
    ("6900", "Other expenses", "expense", "62", False, None),
    ("68", "Finance costs and tax", "expense", "6", True, None),
    ("6800", "Interest expense", "expense", "68", False, None),
    ("6950", "Income tax expense", "expense", "68", False, None),
]


def _seed() -> None:
    conn = op.get_bind()
    add_permissions([(k, s) for k, s, _ in PERMISSIONS])
    for template_key, grants in TEMPLATE_GRANTS.items():
        for key, scope in grants.items():
            conn.execute(
                sa.text(
                    "INSERT INTO role_grants (role_id, permission_key, scope) "
                    "SELECT id, :k, :s FROM roles WHERE template_key = :t ON CONFLICT DO NOTHING"
                ),
                {"k": key, "s": scope, "t": template_key},
            )
    bump_authz_version()
    conn.execute(
        sa.text(
            "INSERT INTO number_sequences (key, prefix, padding, next_value) "
            "VALUES ('journal', 'JV-', 5, 1)"
        )
    )
    seed_defaults(conn)


def seed_defaults(conn: sa.Connection) -> None:
    """The editable defaults. Tests call this to restore them after a reset."""
    conn.execute(
        sa.text("INSERT INTO finance_settings (id, retained_earnings_code) VALUES (1, '3200')")
    )
    for code, name, kind, parent, header, cash_flow in CHART:
        conn.execute(
            sa.text(
                "INSERT INTO finance_accounts "
                "(id, code, name, type, parent_id, is_header, cash_flow) "
                "VALUES (gen_random_uuid(), :c, :n, :t, "
                "(SELECT id FROM finance_accounts WHERE code = :p), :h, :f)"
            ),
            {"c": code, "n": name, "t": kind, "p": parent, "h": header, "f": cash_flow},
        )


def _unseed() -> None:
    keys = [k for k, _, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    op.execute(sa.text("DELETE FROM number_sequences WHERE key = 'journal'"))
    bump_authz_version()


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "finance_accounts",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("is_header", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("cash_flow", sa.String(length=10), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "cash_flow IS NULL OR cash_flow IN ('cash', 'operating', 'investing', 'financing')",
            name=op.f("ck_finance_accounts_cash_flow"),
        ),
        sa.CheckConstraint(
            "type IN ('asset', 'liability', 'equity', 'income', 'expense')",
            name=op.f("ck_finance_accounts_type"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["finance_accounts.id"],
            name=op.f("fk_finance_accounts_parent_id_finance_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finance_accounts")),
        sa.UniqueConstraint("code", name=op.f("uq_finance_accounts_code")),
    )
    op.create_index(
        op.f("ix_finance_accounts_parent_id"), "finance_accounts", ["parent_id"], unique=False
    )
    op.create_table(
        "finance_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("retained_earnings_code", sa.String(length=20), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_finance_settings_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finance_settings")),
    )
    op.create_table(
        "fiscal_years",
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=6), server_default="open", nullable=False),
        sa.Column("opened_by", sa.Uuid(), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_fiscal_years_status")),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_fiscal_years_closed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["opened_by"], ["users.id"], name=op.f("fk_fiscal_years_opened_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiscal_years")),
        sa.UniqueConstraint("year", name=op.f("uq_fiscal_years_year")),
    )
    op.create_table(
        "recurring_journals",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("memo", sa.String(length=200), nullable=False),
        sa.Column("day_of_month", sa.SmallInteger(), nullable=False),
        sa.Column("first_month", sa.Date(), nullable=False),
        sa.Column("last_month", sa.Date(), nullable=True),
        sa.Column("next_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("day_of_month BETWEEN 1 AND 28", name=op.f("ck_recurring_journals_day")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_recurring_journals_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_journals")),
    )
    op.create_table(
        "fiscal_periods",
        sa.Column("year_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.SmallInteger(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=6), server_default="open", nullable=False),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_by", sa.Uuid(), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopen_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_fiscal_periods_status")),
        sa.CheckConstraint("number BETWEEN 1 AND 12", name=op.f("ck_fiscal_periods_number")),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_fiscal_periods_closed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["reopened_by"], ["users.id"], name=op.f("fk_fiscal_periods_reopened_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["year_id"], ["fiscal_years.id"], name=op.f("fk_fiscal_periods_year_id_fiscal_years")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiscal_periods")),
        sa.UniqueConstraint("year_id", "number", name=op.f("uq_fiscal_periods_year_id_number")),
    )
    op.create_index(
        op.f("ix_fiscal_periods_start_date"), "fiscal_periods", ["start_date"], unique=False
    )
    op.create_index(op.f("ix_fiscal_periods_year_id"), "fiscal_periods", ["year_id"], unique=False)
    op.create_table(
        "recurring_journal_lines",
        sa.Column("recurring_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("debit", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("credit", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(debit = 0) <> (credit = 0)", name=op.f("ck_recurring_journal_lines_one_side")
        ),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f("ck_recurring_journal_lines_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_recurring_journal_lines_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_recurring_journal_lines_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["recurring_id"],
            ["recurring_journals.id"],
            name=op.f("fk_recurring_journal_lines_recurring_id_recurring_journals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_journal_lines")),
    )
    op.create_index(
        op.f("ix_recurring_journal_lines_recurring_id"),
        "recurring_journal_lines",
        ["recurring_id"],
        unique=False,
    )
    op.create_table(
        "journals",
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("journal_date", sa.Date(), nullable=False),
        sa.Column("original_date", sa.Date(), nullable=True),
        sa.Column("period_id", sa.Uuid(), nullable=True),
        sa.Column("memo", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=6), server_default="draft", nullable=False),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("redate_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("posted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverses_id", sa.Uuid(), nullable=True),
        sa.Column("reversed_by_id", sa.Uuid(), nullable=True),
        sa.Column("recurring_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'posted') = (number IS NOT NULL)", name=op.f("ck_journals_numbered")
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'recurring', 'reversal', 'year_end', 'payroll', 'expenses')",
            name=op.f("ck_journals_source"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'held', 'posted')", name=op.f("ck_journals_status")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_journals_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["fiscal_periods.id"], name=op.f("fk_journals_period_id_fiscal_periods")
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"], ["users.id"], name=op.f("fk_journals_posted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["recurring_id"],
            ["recurring_journals.id"],
            name=op.f("fk_journals_recurring_id_recurring_journals"),
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_id"], ["journals.id"], name=op.f("fk_journals_reversed_by_id_journals")
        ),
        sa.ForeignKeyConstraint(
            ["reverses_id"], ["journals.id"], name=op.f("fk_journals_reverses_id_journals")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journals")),
        sa.UniqueConstraint("number", name=op.f("uq_journals_number")),
    )
    op.create_index(op.f("ix_journals_journal_date"), "journals", ["journal_date"], unique=False)
    op.create_index(
        "uq_journal_source",
        "journals",
        ["source", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_table(
        "journal_lines",
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("account_code", sa.String(length=20), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("debit", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("credit", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="NGN", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("(debit = 0) <> (credit = 0)", name=op.f("ck_journal_lines_one_side")),
        sa.CheckConstraint("debit >= 0 AND credit >= 0", name=op.f("ck_journal_lines_amounts")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_journal_lines_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_journal_lines_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"],
            ["journals.id"],
            name=op.f("fk_journal_lines_journal_id_journals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_lines")),
    )
    op.create_index(
        op.f("ix_journal_lines_account_id"), "journal_lines", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_journal_lines_journal_id"), "journal_lines", ["journal_id"], unique=False
    )
    # ### end Alembic commands ###
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_journal_lines_journal_id"), table_name="journal_lines")
    op.drop_index(op.f("ix_journal_lines_account_id"), table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index(
        "uq_journal_source",
        table_name="journals",
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_journals_journal_date"), table_name="journals")
    op.drop_table("journals")
    op.drop_index(
        op.f("ix_recurring_journal_lines_recurring_id"), table_name="recurring_journal_lines"
    )
    op.drop_table("recurring_journal_lines")
    op.drop_index(op.f("ix_fiscal_periods_year_id"), table_name="fiscal_periods")
    op.drop_index(op.f("ix_fiscal_periods_start_date"), table_name="fiscal_periods")
    op.drop_table("fiscal_periods")
    op.drop_table("recurring_journals")
    op.drop_table("fiscal_years")
    op.drop_table("finance_settings")
    op.drop_index(op.f("ix_finance_accounts_parent_id"), table_name="finance_accounts")
    op.drop_table("finance_accounts")
    # ### end Alembic commands ###
