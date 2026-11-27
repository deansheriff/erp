"""Inventory (Phase 11a): units, categories and items; warehouses, bins, lots and serial
numbers; stock balances, values and FIFO layers; movement documents, counts, material
requests and the stock ledger (ADR 0028).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG, TEAM, SELF = "organization", "team", "self"
# Frozen copy of the Phase 11a catalogue additions: (key, sensitive).
PERMISSIONS = [
    ("inventory.item.read", False),
    ("inventory.item.manage", False),
    ("inventory.stock.move", False),
    ("inventory.stock.approve", False),
    ("inventory.request.create", False),
    ("inventory.request.read", False),
    ("inventory.request.approve", False),
    ("inventory.request.approve_any", False),
]
INVENTORY_MANAGER = (
    "inventory_manager",
    "Inventory Manager",
    "Keeps items and warehouses, and approves stock adjustments and counts.",
)
TEMPLATE_GRANTS = {
    "organization_administrator": {
        **{key: ORG for key, _ in PERMISSIONS},
        "inventory.request.create": SELF,
    },
    "employee": {"inventory.request.create": SELF, "inventory.request.read": SELF},
    "line_manager": {"inventory.request.read": TEAM, "inventory.request.approve": TEAM},
    "inventory_clerk": {
        "inventory.item.read": ORG,
        "inventory.stock.move": ORG,
        "inventory.request.read": ORG,
    },
    "inventory_manager": {
        "inventory.item.read": ORG,
        "inventory.item.manage": ORG,
        "inventory.stock.move": ORG,
        "inventory.stock.approve": ORG,
        "inventory.request.read": ORG,
        "inventory.request.approve_any": ORG,
    },
    "finance_officer": {"inventory.item.read": ORG},
    "finance_manager": {"inventory.item.read": ORG},
    "auditor": {"inventory.item.read": ORG, "inventory.request.read": ORG},
}
SEQUENCES = [
    ("inventory_item", "ITM-"),
    ("stock_issue", "SI-"),
    ("stock_transfer", "ST-"),
    ("stock_adjustment", "SA-"),
    ("stock_count", "SC-"),
    ("material_request", "MR-"),
]
SOURCES_BEFORE = (
    "'manual', 'recurring', 'reversal', 'year_end', 'payroll', 'expenses', 'receivable', "
    "'payable', 'bank', 'tax', 'asset'"
)
SOURCES_AFTER = SOURCES_BEFORE + ", 'stock'"
# Defaults, all to confirm.
UNITS = [
    ("EA", "Each"),
    ("BOX", "Box"),
    ("CTN", "Carton"),
    ("PCK", "Pack"),
    ("KG", "Kilogram"),
    ("L", "Litre"),
    ("M", "Metre"),
]


def _seed() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO roles (id, name, description, template_key, requires_mfa, protected, "
            "grants_all) VALUES (gen_random_uuid(), :n, :d, :k, false, false, false) "
            "ON CONFLICT DO NOTHING"
        ),
        {"k": INVENTORY_MANAGER[0], "n": INVENTORY_MANAGER[1], "d": INVENTORY_MANAGER[2]},
    )
    add_permissions(PERMISSIONS)
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
    for key, prefix in SEQUENCES:
        conn.execute(
            sa.text(
                "INSERT INTO number_sequences (key, prefix, padding, next_value) "
                "VALUES (:k, :p, 5, 1)"
            ),
            {"k": key, "p": prefix},
        )
    seed_defaults(conn)


def seed_defaults(conn: sa.Connection) -> None:
    """The adjustments account, units, a general category and a main store. Tests call
    this to restore them after a reset."""
    conn.execute(
        sa.text(
            "INSERT INTO finance_accounts (id, code, name, type, parent_id) "
            "VALUES (gen_random_uuid(), '5200', 'Stock adjustments and variances', 'expense', "
            "(SELECT id FROM finance_accounts WHERE code = '5')) ON CONFLICT (code) DO NOTHING"
        )
    )
    for code, name in UNITS:
        conn.execute(
            sa.text(
                "INSERT INTO inventory_units (id, code, name) VALUES (gen_random_uuid(), :c, :n)"
            ),
            {"c": code, "n": name},
        )
    conn.execute(
        sa.text(
            "INSERT INTO item_categories (id, name, inventory_account_id, cogs_account_id, "
            "adjustment_account_id, expense_account_id) SELECT gen_random_uuid(), 'General', "
            "(SELECT id FROM finance_accounts WHERE code = '1200'), "
            "(SELECT id FROM finance_accounts WHERE code = '5100'), "
            "(SELECT id FROM finance_accounts WHERE code = '5200'), "
            "(SELECT id FROM finance_accounts WHERE code = '6300')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO warehouses (id, code, name) VALUES (gen_random_uuid(), 'MAIN', "
            "'Main store')"
        )
    )


def _unseed() -> None:
    keys = [k for k, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    op.execute(
        sa.text("DELETE FROM number_sequences WHERE key = ANY(:keys)").bindparams(
            keys=[k for k, _ in SEQUENCES]
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM role_assignments WHERE role_id IN "
            "(SELECT id FROM roles WHERE template_key = 'inventory_manager')"
        )
    )
    op.execute(sa.text("DELETE FROM roles WHERE template_key = 'inventory_manager'"))
    bump_authz_version()


def _sources(values: str) -> None:
    op.drop_constraint(op.f("ck_journals_source"), "journals", type_="check")
    op.create_check_constraint(op.f("ck_journals_source"), "journals", f"source IN ({values})")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "inventory_units",
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_units")),
        sa.UniqueConstraint("code", name=op.f("uq_inventory_units_code")),
    )
    op.create_table(
        "warehouses",
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouses")),
        sa.UniqueConstraint("code", name=op.f("uq_warehouses_code")),
    )
    op.create_table(
        "item_categories",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("inventory_account_id", sa.Uuid(), nullable=False),
        sa.Column("cogs_account_id", sa.Uuid(), nullable=False),
        sa.Column("adjustment_account_id", sa.Uuid(), nullable=False),
        sa.Column("expense_account_id", sa.Uuid(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["adjustment_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_item_categories_adjustment_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cogs_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_item_categories_cogs_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["expense_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_item_categories_expense_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["inventory_account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_item_categories_inventory_account_id_finance_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_categories")),
        sa.UniqueConstraint("name", name=op.f("uq_item_categories_name")),
    )
    op.create_table(
        "warehouse_bins",
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_warehouse_bins_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouse_bins")),
        sa.UniqueConstraint(
            "warehouse_id", "code", name=op.f("uq_warehouse_bins_warehouse_id_code")
        ),
    )
    op.create_index(
        op.f("ix_warehouse_bins_warehouse_id"), "warehouse_bins", ["warehouse_id"], unique=False
    )
    op.create_table(
        "items",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("valuation", sa.String(length=7), server_default="average", nullable=False),
        sa.Column("tracking", sa.String(length=6), server_default="none", nullable=False),
        sa.Column(
            "reorder_level", sa.Numeric(precision=14, scale=3), server_default="0", nullable=False
        ),
        sa.Column(
            "reorder_quantity",
            sa.Numeric(precision=14, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("tracking IN ('none', 'lot', 'serial')", name=op.f("ck_items_tracking")),
        sa.CheckConstraint("valuation IN ('fifo', 'average')", name=op.f("ck_items_valuation")),
        sa.CheckConstraint(
            "reorder_level >= 0 AND reorder_quantity >= 0", name=op.f("ck_items_reorder")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["item_categories.id"],
            name=op.f("fk_items_category_id_item_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["inventory_units.id"], name=op.f("fk_items_unit_id_inventory_units")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("code", name=op.f("uq_items_code")),
    )
    op.create_index(op.f("ix_items_category_id"), "items", ["category_id"], unique=False)
    op.create_table(
        "material_requests",
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("needed_by", sa.Date(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=9), server_default="draft", nullable=False),
        sa.Column("approver_employee_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'draft') = (number IS NULL)", name=op.f("ck_material_requests_numbered")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'issued', 'cancelled')",
            name=op.f("ck_material_requests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["approver_employee_id"],
            ["employees.id"],
            name=op.f("fk_material_requests_approver_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_material_requests_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_material_requests_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_material_requests_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_material_requests_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_material_requests_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_requests")),
        sa.UniqueConstraint("number", name=op.f("uq_material_requests_number")),
    )
    op.create_index(
        op.f("ix_material_requests_employee_id"), "material_requests", ["employee_id"], unique=False
    )
    op.create_table(
        "item_values",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column(
            "quantity", sa.Numeric(precision=14, scale=3), server_default="0", nullable=False
        ),
        sa.Column("value", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("quantity >= 0 AND value >= 0", name=op.f("ck_item_values_amounts")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_values_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"], ["warehouses.id"], name=op.f("fk_item_values_warehouse_id_warehouses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_values")),
        sa.UniqueConstraint(
            "item_id", "warehouse_id", name=op.f("uq_item_values_item_id_warehouse_id")
        ),
    )
    op.create_index(op.f("ix_item_values_item_id"), "item_values", ["item_id"], unique=False)
    op.create_table(
        "low_stock_alerts",
        sa.Column("alert_date", sa.Date(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_low_stock_alerts_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_low_stock_alerts_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_low_stock_alerts")),
        sa.UniqueConstraint(
            "alert_date",
            "item_id",
            "warehouse_id",
            name=op.f("uq_low_stock_alerts_alert_date_item_id_warehouse_id"),
        ),
    )
    op.create_table(
        "material_request_lines",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("issued", sa.Numeric(precision=14, scale=3), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "quantity > 0 AND issued >= 0", name=op.f("ck_material_request_lines_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_material_request_lines_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["material_requests.id"],
            name=op.f("fk_material_request_lines_request_id_material_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_request_lines")),
    )
    op.create_index(
        op.f("ix_material_request_lines_request_id"),
        "material_request_lines",
        ["request_id"],
        unique=False,
    )
    op.create_table(
        "stock_lots",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=60), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_lots_item_id_items")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_lots")),
        sa.UniqueConstraint("item_id", "number", name=op.f("uq_stock_lots_item_id_number")),
    )
    op.create_index(op.f("ix_stock_lots_item_id"), "stock_lots", ["item_id"], unique=False)
    op.create_table(
        "stock_serials",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=80), nullable=False),
        sa.Column("in_stock", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=True),
        sa.Column("bin_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "in_stock = (warehouse_id IS NOT NULL)", name=op.f("ck_stock_serials_placed")
        ),
        sa.ForeignKeyConstraint(
            ["bin_id"], ["warehouse_bins.id"], name=op.f("fk_stock_serials_bin_id_warehouse_bins")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_serials_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_serials_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_serials")),
        sa.UniqueConstraint("item_id", "number", name=op.f("uq_stock_serials_item_id_number")),
    )
    op.create_index(op.f("ix_stock_serials_item_id"), "stock_serials", ["item_id"], unique=False)
    op.create_table(
        "cost_layers",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("lot_id", sa.Uuid(), nullable=True),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("quantity_left", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=16, scale=4), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "quantity_left >= 0 AND unit_cost >= 0", name=op.f("ck_cost_layers_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_cost_layers_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["stock_lots.id"], name=op.f("fk_cost_layers_lot_id_stock_lots")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"], ["warehouses.id"], name=op.f("fk_cost_layers_warehouse_id_warehouses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_layers")),
    )
    op.create_index(op.f("ix_cost_layers_item_id"), "cost_layers", ["item_id"], unique=False)
    op.create_table(
        "stock_balances",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("bin_id", sa.Uuid(), nullable=True),
        sa.Column("lot_id", sa.Uuid(), nullable=True),
        sa.Column(
            "quantity", sa.Numeric(precision=14, scale=3), server_default="0", nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_stock_balances_quantity")),
        sa.ForeignKeyConstraint(
            ["bin_id"], ["warehouse_bins.id"], name=op.f("fk_stock_balances_bin_id_warehouse_bins")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_balances_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["stock_lots.id"], name=op.f("fk_stock_balances_lot_id_stock_lots")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_balances_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_balances")),
    )
    op.create_index(op.f("ix_stock_balances_item_id"), "stock_balances", ["item_id"], unique=False)
    op.create_index(
        "uq_stock_balance_place",
        "stock_balances",
        ["item_id", "warehouse_id", "bin_id", "lot_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "stock_counts",
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("count_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=9), server_default="counting", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("value", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('counting', 'submitted', 'posted')", name=op.f("ck_stock_counts_status")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_stock_counts_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["journals.id"], name=op.f("fk_stock_counts_journal_id_journals")
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"], ["users.id"], name=op.f("fk_stock_counts_posted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_counts_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_counts")),
        sa.UniqueConstraint("number", name=op.f("uq_stock_counts_number")),
    )
    op.create_table(
        "stock_documents",
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=9), server_default="draft", nullable=False),
        sa.Column("doc_date", sa.Date(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("to_warehouse_id", sa.Uuid(), nullable=True),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("value", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'posted') = (number IS NOT NULL)", name=op.f("ck_stock_documents_numbered")
        ),
        sa.CheckConstraint(
            "kind IN ('issue', 'transfer', 'adjustment')", name=op.f("ck_stock_documents_kind")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'posted')", name=op.f("ck_stock_documents_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_stock_documents_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_stock_documents_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_stock_documents_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["journals.id"], name=op.f("fk_stock_documents_journal_id_journals")
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"], ["users.id"], name=op.f("fk_stock_documents_posted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["material_requests.id"],
            name=op.f("fk_stock_documents_request_id_material_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_documents_to_warehouse_id_warehouses"),
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_documents_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_documents")),
        sa.UniqueConstraint("number", name=op.f("uq_stock_documents_number")),
    )
    op.create_index(op.f("ix_stock_documents_kind"), "stock_documents", ["kind"], unique=False)
    op.create_table(
        "stock_moves",
        sa.Column("move_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("bin_id", sa.Uuid(), nullable=True),
        sa.Column("lot_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("value", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("serials", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('issue', 'transfer_out', 'transfer_in', 'adjustment_in', 'adjustment_out', 'count_in', 'count_out')",
            name=op.f("ck_stock_moves_kind"),
        ),
        sa.CheckConstraint("quantity <> 0", name=op.f("ck_stock_moves_quantity")),
        sa.ForeignKeyConstraint(
            ["bin_id"], ["warehouse_bins.id"], name=op.f("fk_stock_moves_bin_id_warehouse_bins")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_stock_moves_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_moves_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["journals.id"], name=op.f("fk_stock_moves_journal_id_journals")
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["stock_lots.id"], name=op.f("fk_stock_moves_lot_id_stock_lots")
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"], ["warehouses.id"], name=op.f("fk_stock_moves_warehouse_id_warehouses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_moves")),
    )
    op.create_index(
        "ix_stock_moves_item_date", "stock_moves", ["item_id", "move_date"], unique=False
    )
    op.create_table(
        "stock_count_lines",
        sa.Column("count_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("bin_id", sa.Uuid(), nullable=True),
        sa.Column("lot_id", sa.Uuid(), nullable=True),
        sa.Column("expected", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("counted", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("value", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "expected >= 0 AND (counted IS NULL OR counted >= 0)",
            name=op.f("ck_stock_count_lines_amounts"),
        ),
        sa.ForeignKeyConstraint(
            ["bin_id"],
            ["warehouse_bins.id"],
            name=op.f("fk_stock_count_lines_bin_id_warehouse_bins"),
        ),
        sa.ForeignKeyConstraint(
            ["count_id"],
            ["stock_counts.id"],
            name=op.f("fk_stock_count_lines_count_id_stock_counts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_count_lines_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["stock_lots.id"], name=op.f("fk_stock_count_lines_lot_id_stock_lots")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_count_lines")),
    )
    op.create_index(
        op.f("ix_stock_count_lines_count_id"), "stock_count_lines", ["count_id"], unique=False
    )
    op.create_table(
        "stock_document_lines",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=16, scale=4), nullable=True),
        sa.Column("bin_id", sa.Uuid(), nullable=True),
        sa.Column("to_bin_id", sa.Uuid(), nullable=True),
        sa.Column("lot_number", sa.String(length=60), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("serials", sa.Text(), nullable=True),
        sa.Column("request_line_id", sa.Uuid(), nullable=True),
        sa.Column("value", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("quantity <> 0", name=op.f("ck_stock_document_lines_quantity")),
        sa.ForeignKeyConstraint(
            ["bin_id"],
            ["warehouse_bins.id"],
            name=op.f("fk_stock_document_lines_bin_id_warehouse_bins"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["stock_documents.id"],
            name=op.f("fk_stock_document_lines_document_id_stock_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_document_lines_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["request_line_id"],
            ["material_request_lines.id"],
            name=op.f("fk_stock_document_lines_request_line_id_material_request_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["to_bin_id"],
            ["warehouse_bins.id"],
            name=op.f("fk_stock_document_lines_to_bin_id_warehouse_bins"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_document_lines")),
    )
    op.create_index(
        op.f("ix_stock_document_lines_document_id"),
        "stock_document_lines",
        ["document_id"],
        unique=False,
    )
    # ### end Alembic commands ###
    _sources(SOURCES_AFTER)
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_stock_document_lines_document_id"), table_name="stock_document_lines")
    op.drop_table("stock_document_lines")
    op.drop_index(op.f("ix_stock_count_lines_count_id"), table_name="stock_count_lines")
    op.drop_table("stock_count_lines")
    op.drop_index("ix_stock_moves_item_date", table_name="stock_moves")
    op.drop_table("stock_moves")
    op.drop_index(op.f("ix_stock_documents_kind"), table_name="stock_documents")
    op.drop_table("stock_documents")
    op.drop_table("stock_counts")
    op.drop_index(
        "uq_stock_balance_place", table_name="stock_balances", postgresql_nulls_not_distinct=True
    )
    op.drop_index(op.f("ix_stock_balances_item_id"), table_name="stock_balances")
    op.drop_table("stock_balances")
    op.drop_index(op.f("ix_cost_layers_item_id"), table_name="cost_layers")
    op.drop_table("cost_layers")
    op.drop_index(op.f("ix_stock_serials_item_id"), table_name="stock_serials")
    op.drop_table("stock_serials")
    op.drop_index(op.f("ix_stock_lots_item_id"), table_name="stock_lots")
    op.drop_table("stock_lots")
    op.drop_index(op.f("ix_material_request_lines_request_id"), table_name="material_request_lines")
    op.drop_table("material_request_lines")
    op.drop_table("low_stock_alerts")
    op.drop_index(op.f("ix_item_values_item_id"), table_name="item_values")
    op.drop_table("item_values")
    op.drop_index(op.f("ix_material_requests_employee_id"), table_name="material_requests")
    op.drop_table("material_requests")
    op.drop_index(op.f("ix_items_category_id"), table_name="items")
    op.drop_table("items")
    op.drop_index(op.f("ix_warehouse_bins_warehouse_id"), table_name="warehouse_bins")
    op.drop_table("warehouse_bins")
    op.drop_table("item_categories")
    op.drop_table("warehouses")
    op.drop_table("inventory_units")
    # ### end Alembic commands ###
    op.execute("UPDATE journals SET reversed_by_id = NULL WHERE source = 'stock'")
    op.execute(
        "DELETE FROM journals WHERE reverses_id IN (SELECT id FROM journals WHERE source = 'stock')"
    )
    op.execute("DELETE FROM journals WHERE source = 'stock'")
    _sources(SOURCES_BEFORE)
    op.execute(
        "DELETE FROM finance_accounts WHERE code = '5200' AND id NOT IN "
        "(SELECT account_id FROM journal_lines WHERE account_id IS NOT NULL)"
    )
