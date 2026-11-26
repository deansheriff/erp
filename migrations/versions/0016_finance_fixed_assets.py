"""Finance (fifth gate): asset categories, the fixed asset register, revaluations and
disposals, and monthly depreciation runs (ADR 0027).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-26 00:41:10.762894
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = "organization"
# Frozen copy of the Phase 10e catalogue additions: (key, sensitive, widest scope).
PERMISSIONS = [
    ("finance.asset.read", False, ORG),
    ("finance.asset.manage", False, ORG),
    ("finance.asset.approve", False, ORG),
]
TEMPLATE_GRANTS = {
    "organization_administrator": {key: ORG for key, _, _ in PERMISSIONS},
    "finance_officer": {"finance.asset.read": ORG, "finance.asset.manage": ORG},
    "finance_manager": {key: ORG for key, _, _ in PERMISSIONS},
    "auditor": {"finance.asset.read": ORG},
}
SOURCES_BEFORE = (
    "'manual', 'recurring', 'reversal', 'year_end', 'payroll', 'expenses', 'receivable', "
    "'payable', 'bank', 'tax'"
)
SOURCES_AFTER = SOURCES_BEFORE + ", 'asset'"
# (code, name, type, parent, cash-flow class), all to confirm.
ACCOUNTS = [
    ("1511", "Accumulated depreciation \u2013 land and buildings", "asset", "15", "operating"),
    ("1521", "Accumulated depreciation \u2013 plant and machinery", "asset", "15", "operating"),
    ("1531", "Accumulated depreciation \u2013 motor vehicles", "asset", "15", "operating"),
    ("1541", "Accumulated depreciation \u2013 furniture and fittings", "asset", "15", "operating"),
    ("1551", "Accumulated depreciation \u2013 computer equipment", "asset", "15", "operating"),
    ("3300", "Revaluation reserve", "equity", "3", "financing"),
    ("4910", "Gain or loss on disposal of assets", "income", "4", None),
    ("6910", "Revaluation losses", "expense", "62", None),
]
# (name, method, life in months, cost, accumulated depreciation, expense), all to confirm.
CATEGORIES = [
    ("Buildings", "straight_line", 600, "1510", "1511", "6500"),
    ("Land", "none", None, "1510", None, None),
    ("Plant and machinery", "straight_line", 120, "1520", "1521", "6500"),
    ("Motor vehicles", "straight_line", 48, "1530", "1531", "6500"),
    ("Furniture and fittings", "straight_line", 60, "1540", "1541", "6500"),
    ("Computer equipment", "straight_line", 36, "1550", "1551", "6500"),
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
            "VALUES ('fixed_asset', 'FA-', 5, 1)"
        )
    )
    seed_defaults(conn)


def seed_defaults(conn: sa.Connection) -> None:
    """The new accounts and the asset categories. Tests call this to restore them after
    a reset."""
    for code, name, kind, parent, cash_flow in ACCOUNTS:
        conn.execute(
            sa.text(
                "INSERT INTO finance_accounts (id, code, name, type, parent_id, cash_flow) "
                "VALUES (gen_random_uuid(), :c, :n, :t, "
                "(SELECT id FROM finance_accounts WHERE code = :p), :f) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"c": code, "n": name, "t": kind, "p": parent, "f": cash_flow},
        )
    for name, method, life, cost, accumulated, expense in CATEGORIES:
        conn.execute(
            sa.text(
                "INSERT INTO asset_categories (id, name, method, life_months, cost_account_id, "
                "accumulated_account_id, expense_account_id) VALUES (gen_random_uuid(), :n, :m, "
                ":l, (SELECT id FROM finance_accounts WHERE code = :c), "
                "(SELECT id FROM finance_accounts WHERE code = :a), "
                "(SELECT id FROM finance_accounts WHERE code = :e))"
            ),
            {"n": name, "m": method, "l": life, "c": cost, "a": accumulated, "e": expense},
        )


def _unseed() -> None:
    keys = [k for k, _, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    op.execute(sa.text("DELETE FROM number_sequences WHERE key = 'fixed_asset'"))
    bump_authz_version()


def _sources(values: str) -> None:
    op.drop_constraint(op.f("ck_journals_source"), "journals", type_="check")
    op.create_check_constraint(op.f("ck_journals_source"), "journals", f"source IN ({values})")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "asset_categories",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("life_months", sa.SmallInteger(), nullable=True),
        sa.Column("rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("cost_account_id", sa.Uuid(), nullable=False),
        sa.Column("accumulated_account_id", sa.Uuid(), nullable=True),
        sa.Column("expense_account_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(method <> 'straight_line' OR life_months > 0) AND (method <> 'reducing_balance' OR (rate > 0 AND rate <= 100))",
            name=op.f("ck_asset_categories_method_figures"),
        ),
        sa.CheckConstraint(
            "method = 'none' OR (accumulated_account_id IS NOT NULL AND expense_account_id IS NOT NULL)",
            name=op.f("ck_asset_categories_accounts"),
        ),
        sa.CheckConstraint(
            "method IN ('straight_line', 'reducing_balance', 'none')",
            name=op.f("ck_asset_categories_method"),
        ),
        sa.ForeignKeyConstraint(
            ["accumulated_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_asset_categories_accumulated_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_asset_categories_cost_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["expense_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_asset_categories_expense_account_id_finance_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_categories")),
        sa.UniqueConstraint("name", name=op.f("uq_asset_categories_name")),
    )
    op.create_table(
        "depreciation_runs",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=6), server_default="draft", nullable=False),
        sa.Column("total", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("prepared_by", sa.Uuid(), nullable=True),
        sa.Column(
            "prepared_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("posted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'posted')", name=op.f("ck_depreciation_runs_status")
        ),
        sa.CheckConstraint("EXTRACT(DAY FROM month) = 1", name=op.f("ck_depreciation_runs_month")),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["journals.id"], name=op.f("fk_depreciation_runs_journal_id_journals")
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"], ["users.id"], name=op.f("fk_depreciation_runs_posted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["prepared_by"], ["users.id"], name=op.f("fk_depreciation_runs_prepared_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_depreciation_runs")),
        sa.UniqueConstraint("month", name=op.f("uq_depreciation_runs_month")),
    )
    op.create_table(
        "fixed_assets",
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("serial", sa.String(length=120), nullable=True),
        sa.Column("location", sa.String(length=120), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("life_months", sa.SmallInteger(), nullable=True),
        sa.Column("rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("cost_account_id", sa.Uuid(), nullable=False),
        sa.Column("accumulated_account_id", sa.Uuid(), nullable=True),
        sa.Column("expense_account_id", sa.Uuid(), nullable=True),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("acquired_on", sa.Date(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("gross", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column(
            "residual", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False
        ),
        sa.Column(
            "accumulated", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False
        ),
        sa.Column("months_left", sa.SmallInteger(), nullable=True),
        sa.Column("depreciated_to", sa.Date(), nullable=True),
        sa.Column("reserve", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column(
            "revaluation_losses",
            sa.Numeric(precision=16, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=8), server_default="active", nullable=False),
        sa.Column("disposed_on", sa.Date(), nullable=True),
        sa.Column("supplier_id", sa.Uuid(), nullable=True),
        sa.Column("bill_line_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(method <> 'straight_line' OR life_months > 0) AND (method <> 'reducing_balance' OR (rate > 0 AND rate <= 100))",
            name=op.f("ck_fixed_assets_method_figures"),
        ),
        sa.CheckConstraint(
            "(status = 'disposed') = (disposed_on IS NOT NULL)",
            name=op.f("ck_fixed_assets_disposed"),
        ),
        sa.CheckConstraint(
            "method IN ('straight_line', 'reducing_balance', 'none')",
            name=op.f("ck_fixed_assets_method"),
        ),
        sa.CheckConstraint("status IN ('active', 'disposed')", name=op.f("ck_fixed_assets_status")),
        sa.CheckConstraint(
            "cost > 0 AND gross >= 0 AND residual >= 0 AND accumulated >= 0 AND reserve >= 0 AND revaluation_losses >= 0",
            name=op.f("ck_fixed_assets_amounts"),
        ),
        sa.ForeignKeyConstraint(
            ["accumulated_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_fixed_assets_accumulated_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["bill_line_id"],
            ["bill_lines.id"],
            name=op.f("fk_fixed_assets_bill_line_id_bill_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["asset_categories.id"],
            name=op.f("fk_fixed_assets_category_id_asset_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_fixed_assets_cost_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_fixed_assets_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_fixed_assets_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["expense_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_fixed_assets_expense_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name=op.f("fk_fixed_assets_supplier_id_suppliers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fixed_assets")),
        sa.UniqueConstraint("bill_line_id", name=op.f("uq_fixed_assets_bill_line_id")),
        sa.UniqueConstraint("number", name=op.f("uq_fixed_assets_number")),
    )
    op.create_index(
        op.f("ix_fixed_assets_category_id"), "fixed_assets", ["category_id"], unique=False
    )
    op.create_table(
        "asset_changes",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=11), nullable=False),
        sa.Column("status", sa.String(length=6), server_default="draft", nullable=False),
        sa.Column("change_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("carrying", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("result", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('revaluation', 'disposal')", name=op.f("ck_asset_changes_kind")
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name=op.f("ck_asset_changes_status")),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_asset_changes_amount")),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name=op.f("fk_asset_changes_approved_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["fixed_assets.id"], name=op.f("fk_asset_changes_asset_id_fixed_assets")
        ),
        sa.ForeignKeyConstraint(
            ["bank_account_id"],
            ["bank_accounts.id"],
            name=op.f("fk_asset_changes_bank_account_id_bank_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_asset_changes_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["journals.id"], name=op.f("fk_asset_changes_journal_id_journals")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_changes")),
    )
    op.create_index(op.f("ix_asset_changes_asset_id"), "asset_changes", ["asset_id"], unique=False)
    op.create_index(
        "uq_asset_change_waiting",
        "asset_changes",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_table(
        "depreciation_lines",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("months", sa.SmallInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("months > 0 AND amount > 0", name=op.f("ck_depreciation_lines_amounts")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["fixed_assets.id"],
            name=op.f("fk_depreciation_lines_asset_id_fixed_assets"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["depreciation_runs.id"],
            name=op.f("fk_depreciation_lines_run_id_depreciation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_depreciation_lines")),
        sa.UniqueConstraint(
            "run_id", "asset_id", name=op.f("uq_depreciation_lines_run_id_asset_id")
        ),
    )
    op.create_index(
        op.f("ix_depreciation_lines_asset_id"), "depreciation_lines", ["asset_id"], unique=False
    )
    op.create_index(
        op.f("ix_depreciation_lines_run_id"), "depreciation_lines", ["run_id"], unique=False
    )
    op.add_column(
        "finance_settings",
        sa.Column(
            "capitalisation_threshold",
            sa.Numeric(precision=16, scale=2),
            server_default="100000",
            nullable=False,
        ),
    )
    op.add_column(
        "finance_settings",
        sa.Column(
            "revaluation_reserve_code", sa.String(length=20), server_default="3300", nullable=False
        ),
    )
    op.add_column(
        "finance_settings",
        sa.Column("disposal_code", sa.String(length=20), server_default="4910", nullable=False),
    )
    op.add_column(
        "finance_settings",
        sa.Column(
            "revaluation_loss_code", sa.String(length=20), server_default="6910", nullable=False
        ),
    )
    # ### end Alembic commands ###
    _sources(SOURCES_AFTER)
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("finance_settings", "revaluation_loss_code")
    op.drop_column("finance_settings", "disposal_code")
    op.drop_column("finance_settings", "revaluation_reserve_code")
    op.drop_column("finance_settings", "capitalisation_threshold")
    op.drop_index(op.f("ix_depreciation_lines_run_id"), table_name="depreciation_lines")
    op.drop_index(op.f("ix_depreciation_lines_asset_id"), table_name="depreciation_lines")
    op.drop_table("depreciation_lines")
    op.drop_index(
        "uq_asset_change_waiting",
        table_name="asset_changes",
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.drop_index(op.f("ix_asset_changes_asset_id"), table_name="asset_changes")
    op.drop_table("asset_changes")
    op.drop_index(op.f("ix_fixed_assets_category_id"), table_name="fixed_assets")
    op.drop_table("fixed_assets")
    op.drop_table("depreciation_runs")
    op.drop_table("asset_categories")
    # ### end Alembic commands ###
    op.execute("UPDATE journals SET reversed_by_id = NULL WHERE source = 'asset'")
    op.execute(
        "DELETE FROM journals WHERE reverses_id IN (SELECT id FROM journals WHERE source = 'asset')"
    )
    op.execute("DELETE FROM journals WHERE source = 'asset'")
    _sources(SOURCES_BEFORE)
    op.execute(
        sa.text(
            "DELETE FROM finance_accounts WHERE code = ANY(:codes) AND id NOT IN "
            "(SELECT account_id FROM journal_lines WHERE account_id IS NOT NULL)"
        ).bindparams(codes=[a[0] for a in ACCOUNTS])
    )
