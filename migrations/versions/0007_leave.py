"""Leave: types, entitlement rules, public holidays, requests, decisions and balance
adjustments (ADR 0018).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19 21:23:08.575629
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG, SELF, TEAM = "organization", "self", "team"
# Frozen copy of the Phase 7a catalogue additions: (key, sensitive, widest scope).
PERMISSIONS = [
    ("leave.request.create", False, ORG),
    ("leave.request.read", False, ORG),
    ("leave.request.approve", False, ORG),
    ("leave.request.approve_hr", False, ORG),
    ("leave.request.cancel", False, ORG),
    ("leave.balance.read", False, ORG),
    ("leave.balance.adjust", True, ORG),
    ("leave.settings.manage", False, ORG),
]
TEMPLATE_GRANTS = {
    "employee": {
        "leave.request.create": SELF,
        "leave.request.read": SELF,
        "leave.balance.read": SELF,
    },
    "line_manager": {
        "leave.request.read": TEAM,
        "leave.request.approve": TEAM,
        "leave.balance.read": TEAM,
    },
    "hr_officer": {
        "leave.request.create": ORG,
        "leave.request.read": ORG,
        "leave.request.approve_hr": ORG,
        "leave.balance.read": ORG,
    },
    "hr_manager": {key: ORG for key, _, _ in PERMISSIONS},
    "auditor": {"leave.request.read": ORG, "leave.balance.read": ORG},
}

# Seeded, editable defaults (confirm before go-live):
# (code, name, paid, counting, has_balance, hr approval, certificate after days, order)
TYPES = [
    ("annual", "Annual leave", True, "working", True, False, None, 1),
    ("sick", "Sick leave", True, "working", True, False, 2, 2),
    ("maternity", "Maternity leave", True, "calendar", True, True, None, 3),
    ("paternity", "Paternity leave", True, "working", True, True, None, 4),
    ("compassionate", "Compassionate leave", True, "working", True, False, None, 5),
    ("study", "Study leave", True, "working", True, True, None, 6),
    ("unpaid", "Unpaid leave", False, "working", False, True, None, 7),
]
# (type code, months of service, days per year)
RULES = [("annual", 12, 6), ("sick", 0, 12), ("maternity", 0, 84)]
# Fixed-date and Easter holidays (confirm); Islamic holidays are added when announced.
HOLIDAYS = [
    ("2026-01-01", "New Year's Day"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-06", "Easter Monday"),
    ("2026-05-01", "Workers' Day"),
    ("2026-06-12", "Democracy Day"),
    ("2026-10-01", "Independence Day"),
    ("2026-12-25", "Christmas Day"),
    ("2026-12-26", "Boxing Day"),
    ("2027-01-01", "New Year's Day"),
    ("2027-03-26", "Good Friday"),
    ("2027-03-29", "Easter Monday"),
    ("2027-05-01", "Workers' Day"),
    ("2027-06-12", "Democracy Day"),
    ("2027-10-01", "Independence Day"),
    ("2027-12-25", "Christmas Day"),
    ("2027-12-26", "Boxing Day"),
]


def _seed() -> None:
    conn = op.get_bind()
    add_permissions(
        [(k, s) for k, s, _ in PERMISSIONS],
        grant_to_templates={"organization_administrator": ORG},
    )
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
    for code, name, paid, counting, has_balance, hr, cert, order in TYPES:
        conn.execute(
            sa.text(
                "INSERT INTO leave_types (id, code, name, paid, counting, has_balance, "
                "requires_hr_approval, certificate_after_days, sort_order) "
                "VALUES (gen_random_uuid(), :c, :n, :p, :ct, :hb, :hr, :cert, :o)"
            ),
            {
                "c": code,
                "n": name,
                "p": paid,
                "ct": counting,
                "hb": has_balance,
                "hr": hr,
                "cert": cert,
                "o": order,
            },
        )
    for code, months, days in RULES:
        conn.execute(
            sa.text(
                "INSERT INTO leave_entitlement_rules (id, leave_type_id, min_service_months, days) "
                "SELECT gen_random_uuid(), id, :m, :d FROM leave_types WHERE code = :c"
            ),
            {"c": code, "m": months, "d": days},
        )
    for day, name in HOLIDAYS:
        conn.execute(
            sa.text(
                "INSERT INTO public_holidays (id, day, name) VALUES (gen_random_uuid(), :d, :n)"
            ),
            {"d": day, "n": name},
        )


def _unseed() -> None:
    keys = [k for k, _, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    bump_authz_version()


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "leave_types",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("paid", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("counting", sa.String(length=10), server_default="working", nullable=False),
        sa.Column("has_balance", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("accrual", sa.String(length=10), server_default="yearly", nullable=False),
        sa.Column(
            "carry_over_max_days",
            sa.Numeric(precision=5, scale=1),
            server_default="0",
            nullable=False,
        ),
        sa.Column("carry_over_expiry_month", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column("requires_hr_approval", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("certificate_after_days", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("accrual IN ('yearly', 'monthly')", name=op.f("ck_leave_types_accrual")),
        sa.CheckConstraint(
            "counting IN ('working', 'calendar')", name=op.f("ck_leave_types_counting")
        ),
        sa.CheckConstraint(
            "carry_over_expiry_month BETWEEN 1 AND 12", name=op.f("ck_leave_types_expiry_month")
        ),
        sa.CheckConstraint("carry_over_max_days >= 0", name=op.f("ck_leave_types_carry_over")),
        sa.CheckConstraint(
            "certificate_after_days IS NULL OR certificate_after_days >= 0",
            name=op.f("ck_leave_types_cert"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_types")),
        sa.UniqueConstraint("code", name=op.f("uq_leave_types_code")),
        sa.UniqueConstraint("name", name=op.f("uq_leave_types_name")),
    )
    op.create_table(
        "public_holidays",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_public_holidays")),
        sa.UniqueConstraint("day", name=op.f("uq_public_holidays_day")),
    )
    op.create_table(
        "leave_entitlement_rules",
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("grade_id", sa.Uuid(), nullable=True),
        sa.Column("min_service_months", sa.Integer(), server_default="0", nullable=False),
        sa.Column("days", sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("days >= 0", name=op.f("ck_leave_entitlement_rules_days")),
        sa.CheckConstraint(
            "min_service_months >= 0", name=op.f("ck_leave_entitlement_rules_service")
        ),
        sa.ForeignKeyConstraint(
            ["grade_id"], ["grades.id"], name=op.f("fk_leave_entitlement_rules_grade_id_grades")
        ),
        sa.ForeignKeyConstraint(
            ["leave_type_id"],
            ["leave_types.id"],
            name=op.f("fk_leave_entitlement_rules_leave_type_id_leave_types"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_entitlement_rules")),
    )
    op.create_index(
        op.f("ix_leave_entitlement_rules_leave_type_id"),
        "leave_entitlement_rules",
        ["leave_type_id"],
        unique=False,
    )
    op.create_table(
        "leave_adjustments",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("days", sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column("kind", sa.String(length=12), server_default="adjustment", nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('adjustment', 'carry_over')", name=op.f("ck_leave_adjustments_kind")
        ),
        sa.CheckConstraint("days <> 0", name=op.f("ck_leave_adjustments_days")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_leave_adjustments_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_leave_adjustments_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["leave_type_id"],
            ["leave_types.id"],
            name=op.f("fk_leave_adjustments_leave_type_id_leave_types"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_adjustments")),
    )
    op.create_index(
        "ix_leave_adjustments_lookup",
        "leave_adjustments",
        ["employee_id", "leave_type_id", "year"],
        unique=False,
    )
    op.create_index(
        "uq_leave_carry_over",
        "leave_adjustments",
        ["employee_id", "leave_type_id", "year"],
        unique=True,
        postgresql_where=sa.text("kind = 'carry_over'"),
    )
    op.create_table(
        "leave_requests",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_half", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("end_half", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("days", sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("step", sa.String(length=8), server_default="manager", nullable=False),
        sa.Column("manager_employee_id", sa.Uuid(), nullable=True),
        sa.Column("certificate_file_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_leave_requests_status"),
        ),
        sa.CheckConstraint("step IN ('manager', 'hr')", name=op.f("ck_leave_requests_step")),
        sa.CheckConstraint("days > 0", name=op.f("ck_leave_requests_days")),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_leave_requests_dates")),
        sa.ForeignKeyConstraint(
            ["cancelled_by"], ["users.id"], name=op.f("fk_leave_requests_cancelled_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["certificate_file_id"],
            ["files.id"],
            name=op.f("fk_leave_requests_certificate_file_id_files"),
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_leave_requests_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["leave_type_id"],
            ["leave_types.id"],
            name=op.f("fk_leave_requests_leave_type_id_leave_types"),
        ),
        sa.ForeignKeyConstraint(
            ["manager_employee_id"],
            ["employees.id"],
            name=op.f("fk_leave_requests_manager_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_leave_requests_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_requests")),
    )
    op.create_index(
        "ix_leave_requests_dates", "leave_requests", ["start_date", "end_date"], unique=False
    )
    op.create_index(
        op.f("ix_leave_requests_employee_id"), "leave_requests", ["employee_id"], unique=False
    )
    op.create_table(
        "leave_decisions",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(length=8), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')", name=op.f("ck_leave_decisions_decision")
        ),
        sa.CheckConstraint("step IN ('manager', 'hr')", name=op.f("ck_leave_decisions_step")),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_leave_decisions_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["leave_requests.id"],
            name=op.f("fk_leave_decisions_request_id_leave_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_decisions")),
    )
    op.create_index(
        op.f("ix_leave_decisions_request_id"), "leave_decisions", ["request_id"], unique=False
    )
    # ### end Alembic commands ###
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_leave_decisions_request_id"), table_name="leave_decisions")
    op.drop_table("leave_decisions")
    op.drop_index(op.f("ix_leave_requests_employee_id"), table_name="leave_requests")
    op.drop_index("ix_leave_requests_dates", table_name="leave_requests")
    op.drop_table("leave_requests")
    op.drop_index(
        "uq_leave_carry_over",
        table_name="leave_adjustments",
        postgresql_where=sa.text("kind = 'carry_over'"),
    )
    op.drop_index("ix_leave_adjustments_lookup", table_name="leave_adjustments")
    op.drop_table("leave_adjustments")
    op.drop_index(
        op.f("ix_leave_entitlement_rules_leave_type_id"), table_name="leave_entitlement_rules"
    )
    op.drop_table("leave_entitlement_rules")
    op.drop_table("public_holidays")
    op.drop_table("leave_types")
    # ### end Alembic commands ###
