"""Payroll (first gate): pay groups, components and salary structures, people's pay and
deductions, effective-dated statutory settings, rent relief, runs and payslips (ADR 0021).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-20 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG, SELF = "organization", "self"
# Frozen copy of the Phase 9a catalogue additions: (key, sensitive, widest scope).
PERMISSIONS = [
    ("payroll.pay.read", True, ORG),
    ("payroll.pay.manage", True, ORG),
    ("payroll.structure.manage", False, ORG),
    ("payroll.settings.manage", False, ORG),
    ("payroll.run.read", True, ORG),
    ("payroll.run.prepare", True, ORG),
    ("payroll.run.review", True, ORG),
    ("payroll.run.approve", True, ORG),
    ("payroll.run.post", True, ORG),
    ("payroll.run.pay", True, ORG),
    ("payroll.payslip.read", True, ORG),
    ("payroll.relief.declare", False, ORG),
    ("payroll.relief.verify", True, ORG),
]
TEMPLATE_GRANTS = {
    "organization_administrator": {key: ORG for key, _, _ in PERMISSIONS},
    "employee": {
        "payroll.pay.read": SELF,
        "payroll.payslip.read": SELF,
        "payroll.relief.declare": SELF,
    },
    "hr_officer": {"payroll.relief.verify": ORG},
    "payroll_officer": {
        "payroll.pay.read": ORG,
        "payroll.pay.manage": ORG,
        "payroll.structure.manage": ORG,
        "payroll.run.read": ORG,
        "payroll.run.prepare": ORG,
        "payroll.run.review": ORG,
        "payroll.payslip.read": ORG,
        "payroll.relief.verify": ORG,
    },
    "finance_officer": {"payroll.run.read": ORG, "payroll.run.pay": ORG},
    "finance_manager": {
        "payroll.run.read": ORG,
        "payroll.run.review": ORG,
        "payroll.run.approve": ORG,
        "payroll.run.post": ORG,
        "payroll.settings.manage": ORG,
    },
    "auditor": {
        "payroll.pay.read": ORG,
        "payroll.run.read": ORG,
        "payroll.payslip.read": ORG,
    },
}
# Seeded, editable defaults (confirm before go-live).
# (code, name, kind, taxable, pensionable, system, order)
COMPONENTS = [
    ("basic", "Basic salary", "earning", True, True, True, 1),
    ("housing", "Housing allowance", "earning", True, True, False, 2),
    ("transport", "Transport allowance", "earning", True, True, False, 3),
    ("meal", "Meal allowance", "earning", True, False, False, 4),
    ("cooperative", "Cooperative", "deduction", False, False, False, 10),
    ("union_dues", "Union dues", "deduction", False, False, False, 11),
]
# Nigeria Tax Act 2025, from 1 January 2026: (annual width or None, rate).
BANDS = [
    (800000, "0"),
    (2200000, "0.15"),
    (9000000, "0.18"),
    (13000000, "0.21"),
    (25000000, "0.23"),
    (None, "0.25"),
]
STATUTORY = {
    "label": "Nigeria Tax Act 2025 (confirm)",
    "rent_relief_rate": "0.20",
    "rent_relief_cap": "500000",
    "pension_employee_rate": "0.08",
    "pension_employer_rate": "0.10",
    "nhf_rate": "0.025",
    "nhis_rate": "0",
    "nsitf_rate": "0.01",
    "itf_rate": "0.01",
    "minimum_wage": "70000",
    "overtime_multiplier": "1.5",
    "hours_per_month": 173,
}


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
    seed_defaults(conn)


def seed_defaults(conn: sa.Connection) -> None:
    """The editable payroll defaults. Tests call this to restore them after a reset."""
    conn.execute(sa.text("INSERT INTO pay_groups (id, name) VALUES (gen_random_uuid(), 'Staff')"))
    for code, name, kind, taxable, pensionable, system, order in COMPONENTS:
        conn.execute(
            sa.text(
                "INSERT INTO pay_components (id, code, name, kind, calculation, taxable, "
                "pensionable, system, sort_order) "
                "VALUES (gen_random_uuid(), :c, :n, :k, 'fixed', :t, :p, :s, :o)"
            ),
            {
                "c": code,
                "n": name,
                "k": kind,
                "t": taxable,
                "p": pensionable,
                "s": system,
                "o": order,
            },
        )
    conn.execute(
        sa.text("INSERT INTO salary_structures (id, name) VALUES (gen_random_uuid(), 'Standard')")
    )
    conn.execute(
        sa.text(
            "INSERT INTO salary_structure_components (id, structure_id, component_id) "
            "SELECT gen_random_uuid(), s.id, c.id FROM salary_structures s, pay_components c "
            "WHERE s.name = 'Standard' AND c.code IN ('basic', 'housing', 'transport')"
        )
    )
    columns = ", ".join(STATUTORY)
    values = ", ".join(f":{k}" for k in STATUTORY)
    conn.execute(
        sa.text(
            f"INSERT INTO statutory_sets (id, effective_from, {columns}) "  # noqa: S608
            f"VALUES (gen_random_uuid(), '2026-01-01', {values})"
        ),
        STATUTORY,
    )
    for position, (width, rate) in enumerate(BANDS, start=1):
        conn.execute(
            sa.text(
                "INSERT INTO tax_bands (id, set_id, position, width, rate) "
                "SELECT gen_random_uuid(), id, :p, :w, :r FROM statutory_sets "
                "WHERE effective_from = '2026-01-01'"
            ),
            {"p": position, "w": width, "r": rate},
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
        "pay_components",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("calculation", sa.String(length=8), server_default="fixed", nullable=False),
        sa.Column("percent", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("base_component_id", sa.Uuid(), nullable=True),
        sa.Column("taxable", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("pensionable", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("system", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "calculation = 'fixed' OR (percent > 0 AND base_component_id IS NOT NULL)",
            name=op.f("ck_pay_components_percent"),
        ),
        sa.CheckConstraint(
            "calculation IN ('fixed', 'percent')", name=op.f("ck_pay_components_calculation")
        ),
        sa.CheckConstraint(
            "kind = 'earning' OR NOT pensionable", name=op.f("ck_pay_components_pensionable")
        ),
        sa.CheckConstraint("kind IN ('earning', 'deduction')", name=op.f("ck_pay_components_kind")),
        sa.ForeignKeyConstraint(
            ["base_component_id"],
            ["pay_components.id"],
            name=op.f("fk_pay_components_base_component_id_pay_components"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_components")),
        sa.UniqueConstraint("code", name=op.f("uq_pay_components_code")),
        sa.UniqueConstraint("name", name=op.f("uq_pay_components_name")),
    )
    op.create_table(
        "pay_groups",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_pay_groups_name")),
    )
    op.create_table(
        "salary_structures",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_structures")),
        sa.UniqueConstraint("name", name=op.f("uq_salary_structures_name")),
    )
    op.create_table(
        "statutory_sets",
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("rent_relief_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("rent_relief_cap", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("pension_employee_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("pension_employer_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("nhf_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("nhis_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("nsitf_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("itf_rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("minimum_wage", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("overtime_multiplier", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("hours_per_month", sa.SmallInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("hours_per_month > 0", name=op.f("ck_statutory_sets_hours")),
        sa.CheckConstraint(
            "pension_employee_rate >= 0 AND pension_employer_rate >= 0 AND nhf_rate >= 0 AND nhis_rate >= 0 AND nsitf_rate >= 0 AND itf_rate >= 0 AND rent_relief_rate >= 0 AND overtime_multiplier >= 0",
            name=op.f("ck_statutory_sets_rates"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_sets")),
        sa.UniqueConstraint("effective_from", name=op.f("uq_statutory_sets_effective_from")),
    )
    op.create_table(
        "payroll_runs",
        sa.Column("pay_group_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="draft", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("calculated_by", sa.Uuid(), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by", sa.Uuid(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_note", sa.Text(), nullable=True),
        sa.Column("statutory_set_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'calculated', 'reviewed', 'approved', 'posted', 'paid')",
            name=op.f("ck_payroll_runs_status"),
        ),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name=op.f("ck_payroll_runs_month")),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name=op.f("fk_payroll_runs_approved_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["calculated_by"], ["users.id"], name=op.f("fk_payroll_runs_calculated_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_payroll_runs_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["paid_by"], ["users.id"], name=op.f("fk_payroll_runs_paid_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["pay_group_id"],
            ["pay_groups.id"],
            name=op.f("fk_payroll_runs_pay_group_id_pay_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"], ["users.id"], name=op.f("fk_payroll_runs_posted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], name=op.f("fk_payroll_runs_reviewed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["statutory_set_id"],
            ["statutory_sets.id"],
            name=op.f("fk_payroll_runs_statutory_set_id_statutory_sets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_runs")),
        sa.UniqueConstraint(
            "pay_group_id", "year", "month", name=op.f("uq_payroll_runs_pay_group_id_year_month")
        ),
    )
    op.create_table(
        "salary_structure_components",
        sa.Column("structure_id", sa.Uuid(), nullable=False),
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("default_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["pay_components.id"],
            name=op.f("fk_salary_structure_components_component_id_pay_components"),
        ),
        sa.ForeignKeyConstraint(
            ["structure_id"],
            ["salary_structures.id"],
            name=op.f("fk_salary_structure_components_structure_id_salary_structures"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_structure_components")),
        sa.UniqueConstraint(
            "structure_id",
            "component_id",
            name=op.f("uq_salary_structure_components_structure_id_component_id"),
        ),
    )
    op.create_table(
        "tax_bands",
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("width", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("rate", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("rate >= 0 AND rate <= 1", name=op.f("ck_tax_bands_rate")),
        sa.CheckConstraint("width IS NULL OR width > 0", name=op.f("ck_tax_bands_width")),
        sa.ForeignKeyConstraint(
            ["set_id"],
            ["statutory_sets.id"],
            name=op.f("fk_tax_bands_set_id_statutory_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tax_bands")),
        sa.UniqueConstraint("set_id", "position", name=op.f("uq_tax_bands_set_id_position")),
    )
    op.create_table(
        "employee_deductions",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("start_month", sa.Date(), nullable=False),
        sa.Column("end_month", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_employee_deductions_amount")),
        sa.CheckConstraint(
            "end_month IS NULL OR start_month <= end_month",
            name=op.f("ck_employee_deductions_months"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["pay_components.id"],
            name=op.f("fk_employee_deductions_component_id_pay_components"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_employee_deductions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_employee_deductions_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_deductions")),
    )
    op.create_index(
        op.f("ix_employee_deductions_employee_id"),
        "employee_deductions",
        ["employee_id"],
        unique=False,
    )
    op.create_table(
        "employee_pay",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("pay_group_id", sa.Uuid(), nullable=False),
        sa.Column("structure_id", sa.Uuid(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date <= end_date", name=op.f("ck_employee_pay_dates")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_employee_pay_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_employee_pay_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pay_group_id"],
            ["pay_groups.id"],
            name=op.f("fk_employee_pay_pay_group_id_pay_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["structure_id"],
            ["salary_structures.id"],
            name=op.f("fk_employee_pay_structure_id_salary_structures"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_pay")),
    )
    op.create_index(
        op.f("ix_employee_pay_employee_id"), "employee_pay", ["employee_id"], unique=False
    )
    op.create_table(
        "payslips",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("days_in_month", sa.SmallInteger(), nullable=False),
        sa.Column("days_paid", sa.SmallInteger(), nullable=False),
        sa.Column("gross", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("taxable", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("paye", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("pension_employee", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("pension_employer", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("nhf", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("nhis", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("nsitf", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("other_deductions", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("net", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("rent_relief", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("ytd_taxable", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("ytd_paye", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("warnings", sa.Text(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_payslips_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_payslips_file_id_files")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_runs.id"],
            name=op.f("fk_payslips_run_id_payroll_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payslips")),
        sa.UniqueConstraint("run_id", "employee_id", name=op.f("uq_payslips_run_id_employee_id")),
    )
    op.create_index(op.f("ix_payslips_employee_id"), "payslips", ["employee_id"], unique=False)
    op.create_table(
        "rent_declarations",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("tax_year", sa.SmallInteger(), nullable=False),
        sa.Column("annual_rent", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("proof_file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("declared_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'rejected')",
            name=op.f("ck_rent_declarations_status"),
        ),
        sa.CheckConstraint("annual_rent > 0", name=op.f("ck_rent_declarations_rent")),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_rent_declarations_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["declared_by"], ["users.id"], name=op.f("fk_rent_declarations_declared_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_rent_declarations_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proof_file_id"], ["files.id"], name=op.f("fk_rent_declarations_proof_file_id_files")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rent_declarations")),
    )
    op.create_index(
        op.f("ix_rent_declarations_employee_id"), "rent_declarations", ["employee_id"], unique=False
    )
    op.create_index(
        "uq_rent_declaration_live",
        "rent_declarations",
        ["employee_id", "tax_year"],
        unique=True,
        postgresql_where=sa.text("status != 'rejected'"),
    )
    op.create_table(
        "employee_pay_amounts",
        sa.Column("pay_id", sa.Uuid(), nullable=False),
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_employee_pay_amounts_amount")),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["pay_components.id"],
            name=op.f("fk_employee_pay_amounts_component_id_pay_components"),
        ),
        sa.ForeignKeyConstraint(
            ["pay_id"],
            ["employee_pay.id"],
            name=op.f("fk_employee_pay_amounts_pay_id_employee_pay"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_pay_amounts")),
        sa.UniqueConstraint(
            "pay_id", "component_id", name=op.f("uq_employee_pay_amounts_pay_id_component_id")
        ),
    )
    op.create_table(
        "payslip_lines",
        sa.Column("payslip_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("detail", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('earning', 'deduction')", name=op.f("ck_payslip_lines_kind")),
        sa.ForeignKeyConstraint(
            ["payslip_id"],
            ["payslips.id"],
            name=op.f("fk_payslip_lines_payslip_id_payslips"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payslip_lines")),
    )
    op.create_index(
        op.f("ix_payslip_lines_payslip_id"), "payslip_lines", ["payslip_id"], unique=False
    )
    # ### end Alembic commands ###
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_payslip_lines_payslip_id"), table_name="payslip_lines")
    op.drop_table("payslip_lines")
    op.drop_table("employee_pay_amounts")
    op.drop_index(
        "uq_rent_declaration_live",
        table_name="rent_declarations",
        postgresql_where=sa.text("status != 'rejected'"),
    )
    op.drop_index(op.f("ix_rent_declarations_employee_id"), table_name="rent_declarations")
    op.drop_table("rent_declarations")
    op.drop_index(op.f("ix_payslips_employee_id"), table_name="payslips")
    op.drop_table("payslips")
    op.drop_index(op.f("ix_employee_pay_employee_id"), table_name="employee_pay")
    op.drop_table("employee_pay")
    op.drop_index(op.f("ix_employee_deductions_employee_id"), table_name="employee_deductions")
    op.drop_table("employee_deductions")
    op.drop_table("tax_bands")
    op.drop_table("salary_structure_components")
    op.drop_table("payroll_runs")
    op.drop_table("statutory_sets")
    op.drop_table("salary_structures")
    op.drop_table("pay_groups")
    op.drop_table("pay_components")
    # ### end Alembic commands ###
