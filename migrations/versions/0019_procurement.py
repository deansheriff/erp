"""Procurement (Phase 11c): requisitions with approval tiers by amount, RFQs, supplier
quotes and awards, and supplier ratings; one optional attachment on outgoing mail
(ADR 0030).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG, TEAM, SELF = "organization", "team", "self"
# Frozen copy of the Phase 11c catalogue additions: (key, sensitive).
PERMISSIONS = [
    ("procurement.requisition.create", False),
    ("procurement.requisition.read", False),
    ("procurement.requisition.approve", False),
    ("procurement.requisition.approve_any", False),
    ("procurement.requisition.approve_finance", False),
    ("procurement.requisition.approve_director", False),
    ("procurement.rfq.read", False),
    ("procurement.rfq.manage", False),
    ("procurement.supplier.rate", False),
    ("procurement.settings.manage", False),
]
DIRECTOR = (
    "director",
    "Director",
    "Approves the largest requisitions and steps in when there's no line manager.",
)
TEMPLATE_GRANTS = {
    "organization_administrator": {key: ORG for key, _ in PERMISSIONS},
    "employee": {
        "procurement.requisition.create": SELF,
        "procurement.requisition.read": SELF,
    },
    "line_manager": {
        "procurement.requisition.read": TEAM,
        "procurement.requisition.approve": TEAM,
    },
    "procurement_officer": {
        "procurement.requisition.read": ORG,
        "procurement.rfq.read": ORG,
        "procurement.rfq.manage": ORG,
        "procurement.supplier.rate": ORG,
        "finance.supplier.read": ORG,
        "finance.purchase.read": ORG,
        "finance.purchase.create": ORG,
        "inventory.item.read": ORG,
    },
    "director": {
        "procurement.requisition.read": ORG,
        "procurement.requisition.approve_director": ORG,
        "procurement.requisition.approve_any": ORG,
        "procurement.rfq.read": ORG,
        "procurement.settings.manage": ORG,
    },
    "finance_manager": {
        "procurement.requisition.read": ORG,
        "procurement.requisition.approve_finance": ORG,
        "procurement.rfq.read": ORG,
        "procurement.settings.manage": ORG,
    },
    "auditor": {"procurement.requisition.read": ORG, "procurement.rfq.read": ORG},
}
SEQUENCES = [("purchase_requisition", "PR-"), ("rfq", "RFQ-")]
# Defaults, all to confirm.
TIERS = [("line_manager", "0"), ("finance", "500000"), ("director", "5000000")]


def _seed() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO roles (id, name, description, template_key, requires_mfa, protected, "
            "grants_all) VALUES (gen_random_uuid(), :n, :d, :k, false, false, false) "
            "ON CONFLICT DO NOTHING"
        ),
        {"k": DIRECTOR[0], "n": DIRECTOR[1], "d": DIRECTOR[2]},
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
    """The quotes threshold and the approval tiers. Tests call this to restore them after
    a reset."""
    conn.execute(
        sa.text(
            "INSERT INTO procurement_settings (id, quote_threshold) VALUES (1, 1000000) "
            "ON CONFLICT DO NOTHING"
        )
    )
    for level, above in TIERS:
        conn.execute(
            sa.text(
                "INSERT INTO procurement_tiers (id, level, above) "
                "VALUES (gen_random_uuid(), :l, :a) ON CONFLICT DO NOTHING"
            ),
            {"l": level, "a": above},
        )


def _unseed() -> None:
    keys = [k for k, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    # The Procurement Officer's grants from other modules (procurement's go with its keys).
    officer = [
        k for k in TEMPLATE_GRANTS["procurement_officer"] if not k.startswith("procurement.")
    ]
    op.execute(
        sa.text(
            "DELETE FROM role_grants WHERE permission_key = ANY(:keys) AND role_id IN "
            "(SELECT id FROM roles WHERE template_key = 'procurement_officer')"
        ).bindparams(keys=officer)
    )
    op.execute(
        sa.text("DELETE FROM number_sequences WHERE key = ANY(:keys)").bindparams(
            keys=[k for k, _ in SEQUENCES]
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM role_assignments WHERE role_id IN "
            "(SELECT id FROM roles WHERE template_key = 'director')"
        )
    )
    op.execute(sa.text("DELETE FROM roles WHERE template_key = 'director'"))
    bump_authz_version()


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "procurement_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "quote_threshold",
            sa.Numeric(precision=16, scale=2),
            server_default="1000000",
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_procurement_settings_one_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procurement_settings")),
    )
    op.create_table(
        "procurement_tiers",
        sa.Column("level", sa.String(length=12), nullable=False),
        sa.Column("above", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "level IN ('line_manager', 'finance', 'director')",
            name=op.f("ck_procurement_tiers_level"),
        ),
        sa.CheckConstraint("above >= 0", name=op.f("ck_procurement_tiers_above")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procurement_tiers")),
        sa.UniqueConstraint("level", name=op.f("uq_procurement_tiers_level")),
    )
    op.create_table(
        "purchase_requisitions",
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("needed_by", sa.Date(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="draft", nullable=False),
        sa.Column(
            "estimated_total", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'draft') = (number IS NULL)", name=op.f("ck_purchase_requisitions_numbered")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'cancelled', 'ordered')",
            name=op.f("ck_purchase_requisitions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_purchase_requisitions_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_purchase_requisitions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_purchase_requisitions_employee_id_employees"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_requisitions")),
        sa.UniqueConstraint("number", name=op.f("uq_purchase_requisitions_number")),
    )
    op.create_index(
        op.f("ix_purchase_requisitions_employee_id"),
        "purchase_requisitions",
        ["employee_id"],
        unique=False,
    )
    op.create_table(
        "requisition_lines",
        sa.Column("requisition_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("warehouse_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("estimated_price", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(item_id IS NULL) = (warehouse_id IS NULL)",
            name=op.f("ck_requisition_lines_stock_place"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND estimated_price >= 0", name=op.f("ck_requisition_lines_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["finance_accounts.id"],
            name=op.f("fk_requisition_lines_account_id_finance_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_requisition_lines_item_id_items")
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"],
            ["purchase_requisitions.id"],
            name=op.f("fk_requisition_lines_requisition_id_purchase_requisitions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_requisition_lines_warehouse_id_warehouses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requisition_lines")),
    )
    op.create_index(
        op.f("ix_requisition_lines_requisition_id"),
        "requisition_lines",
        ["requisition_id"],
        unique=False,
    )
    op.create_table(
        "requisition_steps",
        sa.Column("requisition_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("level", sa.String(length=12), nullable=False),
        sa.Column("approver_employee_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=8), server_default="pending", nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "level IN ('line_manager', 'finance', 'director')",
            name=op.f("ck_requisition_steps_level"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_requisition_steps_status"),
        ),
        sa.ForeignKeyConstraint(
            ["approver_employee_id"],
            ["employees.id"],
            name=op.f("fk_requisition_steps_approver_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_requisition_steps_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"],
            ["purchase_requisitions.id"],
            name=op.f("fk_requisition_steps_requisition_id_purchase_requisitions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requisition_steps")),
        sa.UniqueConstraint(
            "requisition_id", "position", name=op.f("uq_requisition_steps_requisition_id_position")
        ),
    )
    op.create_index(
        op.f("ix_requisition_steps_requisition_id"),
        "requisition_steps",
        ["requisition_id"],
        unique=False,
    )
    op.create_table(
        "rfqs",
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("requisition_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=9), server_default="draft", nullable=False),
        sa.Column("respond_by", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("award_reason", sa.Text(), nullable=True),
        sa.Column("awarded_by", sa.Uuid(), nullable=True),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'awarded', 'ordered', 'cancelled')",
            name=op.f("ck_rfqs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["awarded_by"], ["users.id"], name=op.f("fk_rfqs_awarded_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_rfqs_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["purchase_orders.id"], name=op.f("fk_rfqs_order_id_purchase_orders")
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"],
            ["purchase_requisitions.id"],
            name=op.f("fk_rfqs_requisition_id_purchase_requisitions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rfqs")),
        sa.UniqueConstraint("number", name=op.f("uq_rfqs_number")),
    )
    op.create_index(
        "uq_rfqs_live_requisition",
        "rfqs",
        ["requisition_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_table(
        "supplier_ratings",
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("rated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "rated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("score BETWEEN 1 AND 5", name=op.f("ck_supplier_ratings_score")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_supplier_ratings_order_id_purchase_orders"),
        ),
        sa.ForeignKeyConstraint(
            ["rated_by"], ["users.id"], name=op.f("fk_supplier_ratings_rated_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
            name=op.f("fk_supplier_ratings_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_ratings")),
        sa.UniqueConstraint("order_id", name=op.f("uq_supplier_ratings_order_id")),
    )
    op.create_index(
        op.f("ix_supplier_ratings_supplier_id"), "supplier_ratings", ["supplier_id"], unique=False
    )
    op.create_table(
        "rfq_suppliers",
        sa.Column("rfq_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rfq_id"], ["rfqs.id"], name=op.f("fk_rfq_suppliers_rfq_id_rfqs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name=op.f("fk_rfq_suppliers_supplier_id_suppliers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rfq_suppliers")),
        sa.UniqueConstraint(
            "rfq_id", "supplier_id", name=op.f("uq_rfq_suppliers_rfq_id_supplier_id")
        ),
    )
    op.create_index(op.f("ix_rfq_suppliers_rfq_id"), "rfq_suppliers", ["rfq_id"], unique=False)
    op.create_index(
        op.f("ix_rfq_suppliers_supplier_id"), "rfq_suppliers", ["supplier_id"], unique=False
    )
    op.create_table(
        "supplier_quotes",
        sa.Column("rfq_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(length=80), nullable=True),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("delivery_days", sa.SmallInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("net", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("vat", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("awarded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "delivery_days IS NULL OR delivery_days >= 0", name=op.f("ck_supplier_quotes_delivery")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_supplier_quotes_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_supplier_quotes_file_id_files")
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"], ["rfqs.id"], name=op.f("fk_supplier_quotes_rfq_id_rfqs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name=op.f("fk_supplier_quotes_supplier_id_suppliers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_quotes")),
        sa.UniqueConstraint(
            "rfq_id", "supplier_id", name=op.f("uq_supplier_quotes_rfq_id_supplier_id")
        ),
    )
    op.create_index(op.f("ix_supplier_quotes_rfq_id"), "supplier_quotes", ["rfq_id"], unique=False)
    op.create_index(
        op.f("ix_supplier_quotes_supplier_id"), "supplier_quotes", ["supplier_id"], unique=False
    )
    op.create_table(
        "supplier_quote_lines",
        sa.Column("quote_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("vat_code", sa.String(length=8), nullable=False),
        sa.Column("vat_rate", sa.Numeric(precision=5, scale=2), server_default="0", nullable=False),
        sa.Column("net", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("vat", sa.Numeric(precision=16, scale=2), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_supplier_quote_lines_price")),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["requisition_lines.id"],
            name=op.f("fk_supplier_quote_lines_line_id_requisition_lines"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["supplier_quotes.id"],
            name=op.f("fk_supplier_quote_lines_quote_id_supplier_quotes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_quote_lines")),
        sa.UniqueConstraint(
            "quote_id", "line_id", name=op.f("uq_supplier_quote_lines_quote_id_line_id")
        ),
    )
    op.create_index(
        op.f("ix_supplier_quote_lines_quote_id"), "supplier_quote_lines", ["quote_id"], unique=False
    )
    op.add_column("mail_outbox", sa.Column("attachment_name", sa.String(length=120), nullable=True))
    op.add_column("mail_outbox", sa.Column("attachment_type", sa.String(length=80), nullable=True))
    op.add_column("mail_outbox", sa.Column("attachment_data", sa.LargeBinary(), nullable=True))
    # ### end Alembic commands ###
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("mail_outbox", "attachment_data")
    op.drop_column("mail_outbox", "attachment_type")
    op.drop_column("mail_outbox", "attachment_name")
    op.drop_index(op.f("ix_supplier_quote_lines_quote_id"), table_name="supplier_quote_lines")
    op.drop_table("supplier_quote_lines")
    op.drop_index(op.f("ix_supplier_quotes_supplier_id"), table_name="supplier_quotes")
    op.drop_index(op.f("ix_supplier_quotes_rfq_id"), table_name="supplier_quotes")
    op.drop_table("supplier_quotes")
    op.drop_index(op.f("ix_rfq_suppliers_supplier_id"), table_name="rfq_suppliers")
    op.drop_index(op.f("ix_rfq_suppliers_rfq_id"), table_name="rfq_suppliers")
    op.drop_table("rfq_suppliers")
    op.drop_index(op.f("ix_supplier_ratings_supplier_id"), table_name="supplier_ratings")
    op.drop_table("supplier_ratings")
    op.drop_index(
        "uq_rfqs_live_requisition",
        table_name="rfqs",
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.drop_table("rfqs")
    op.drop_index(op.f("ix_requisition_steps_requisition_id"), table_name="requisition_steps")
    op.drop_table("requisition_steps")
    op.drop_index(op.f("ix_requisition_lines_requisition_id"), table_name="requisition_lines")
    op.drop_table("requisition_lines")
    op.drop_index(op.f("ix_purchase_requisitions_employee_id"), table_name="purchase_requisitions")
    op.drop_table("purchase_requisitions")
    op.drop_table("procurement_tiers")
    op.drop_table("procurement_settings")
    # ### end Alembic commands ###
