"""Payroll (second gate): staff loans, bank codes, ledger accounts and queued journals
(ADR 0022).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-20 10:30:31.500026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG, SELF = "organization", "self"
# Frozen copy of the Phase 9b catalogue additions: (key, sensitive, widest scope).
PERMISSIONS = [
    ("payroll.remittance.export", True, ORG),
    ("payroll.loan.read", True, ORG),
    ("payroll.loan.manage", True, ORG),
    ("payroll.loan.approve", True, ORG),
    ("payroll.certificate.read", True, ORG),
]
TEMPLATE_GRANTS = {
    "organization_administrator": {key: ORG for key, _, _ in PERMISSIONS},
    "employee": {"payroll.loan.read": SELF, "payroll.certificate.read": SELF},
    "payroll_officer": {
        "payroll.remittance.export": ORG,
        "payroll.loan.read": ORG,
        "payroll.loan.manage": ORG,
        "payroll.certificate.read": ORG,
    },
    "finance_officer": {"payroll.remittance.export": ORG, "payroll.loan.read": ORG},
    "finance_manager": {
        "payroll.remittance.export": ORG,
        "payroll.loan.read": ORG,
        "payroll.loan.approve": ORG,
        "payroll.certificate.read": ORG,
    },
    "auditor": {"payroll.loan.read": ORG, "payroll.certificate.read": ORG},
}
# Seeded, editable defaults (confirm before go-live).
ACCOUNTS = {
    "salaries": "6000",
    "employer_pension": "6010",
    "employer_nsitf": "6020",
    "paye_payable": "2110",
    "pension_payable": "2120",
    "nhf_payable": "2130",
    "nhis_payable": "2140",
    "nsitf_payable": "2150",
    "deductions_payable": "2160",
    "net_pay_payable": "2170",
    "staff_loans": "1420",
    "bank": "1010",
}
# (name, CBN or NIP code, other names it goes by)
BANKS = [
    ("Access Bank", "044", "Access, Access Bank Plc, Diamond Bank"),
    ("Citibank Nigeria", "023", "Citibank, Citi"),
    ("Ecobank Nigeria", "050", "Ecobank"),
    ("Fidelity Bank", "070", "Fidelity"),
    ("First Bank of Nigeria", "011", "First Bank, FirstBank, FBN"),
    ("First City Monument Bank", "214", "FCMB"),
    ("Globus Bank", "103", "Globus"),
    ("Guaranty Trust Bank", "058", "GTBank, GTB, GTCO, Guaranty Trust"),
    ("Jaiz Bank", "301", "Jaiz"),
    ("Keystone Bank", "082", "Keystone"),
    ("Lotus Bank", "303", "Lotus"),
    ("Optimus Bank", "107", "Optimus"),
    ("Polaris Bank", "076", "Polaris, Skye Bank"),
    ("PremiumTrust Bank", "105", "Premium Trust Bank, PremiumTrust"),
    ("Providus Bank", "101", "Providus"),
    ("Stanbic IBTC Bank", "221", "Stanbic IBTC, Stanbic"),
    ("Standard Chartered Bank", "068", "Standard Chartered, StanChart"),
    ("Sterling Bank", "232", "Sterling"),
    ("SunTrust Bank", "100", "SunTrust"),
    ("TAJBank", "302", "Taj Bank, TAJ"),
    ("Titan Trust Bank", "102", "Titan Trust, Titan"),
    ("Union Bank of Nigeria", "032", "Union Bank, Union"),
    ("United Bank for Africa", "033", "UBA"),
    ("Unity Bank", "215", "Unity"),
    ("Wema Bank", "035", "Wema, ALAT"),
    ("Zenith Bank", "057", "Zenith"),
    ("Kuda Microfinance Bank", "50211", "Kuda, Kuda Bank"),
    ("Moniepoint Microfinance Bank", "50515", "Moniepoint"),
    ("OPay", "999992", "OPay Digital Services, Paycom"),
    ("PalmPay", "999991", "Palm Pay"),
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
            "VALUES ('staff_loan', 'LN-', 5, 1)"
        )
    )
    seed_defaults(conn)


def seed_defaults(conn: sa.Connection) -> None:
    """The editable defaults. Tests call this to restore them after a reset."""
    columns = ", ".join(ACCOUNTS)
    values = ", ".join(f":{k}" for k in ACCOUNTS)
    conn.execute(
        sa.text(f"INSERT INTO payroll_accounts (id, {columns}) VALUES (1, {values})"),  # noqa: S608
        ACCOUNTS,
    )
    for name, code, other in BANKS:
        conn.execute(
            sa.text(
                "INSERT INTO payroll_banks (id, name, code, other_names) "
                "VALUES (gen_random_uuid(), :n, :c, :o)"
            ),
            {"n": name, "c": code, "o": other},
        )


def _unseed() -> None:
    keys = [k for k, _, _ in PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    op.execute(sa.text("DELETE FROM number_sequences WHERE key = 'staff_loan'"))
    bump_authz_version()


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "payroll_accounts",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("salaries", sa.String(length=20), nullable=False),
        sa.Column("employer_pension", sa.String(length=20), nullable=False),
        sa.Column("employer_nsitf", sa.String(length=20), nullable=False),
        sa.Column("paye_payable", sa.String(length=20), nullable=False),
        sa.Column("pension_payable", sa.String(length=20), nullable=False),
        sa.Column("nhf_payable", sa.String(length=20), nullable=False),
        sa.Column("nhis_payable", sa.String(length=20), nullable=False),
        sa.Column("nsitf_payable", sa.String(length=20), nullable=False),
        sa.Column("deductions_payable", sa.String(length=20), nullable=False),
        sa.Column("net_pay_payable", sa.String(length=20), nullable=False),
        sa.Column("staff_loans", sa.String(length=20), nullable=False),
        sa.Column("bank", sa.String(length=20), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_payroll_accounts_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_accounts")),
    )
    op.create_table(
        "payroll_banks",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("other_names", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_banks")),
        sa.UniqueConstraint("name", name=op.f("uq_payroll_banks_name")),
    )
    op.create_table(
        "staff_loans",
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("instalment", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("first_month", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('loan', 'advance')", name=op.f("ck_staff_loans_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_staff_loans_status"),
        ),
        sa.CheckConstraint(
            "EXTRACT(DAY FROM first_month) = 1", name=op.f("ck_staff_loans_first_month")
        ),
        sa.CheckConstraint(
            "amount > 0 AND instalment > 0 AND instalment <= amount",
            name=op.f("ck_staff_loans_amounts"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_staff_loans_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_staff_loans_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"], ["users.id"], name=op.f("fk_staff_loans_recorded_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_loans")),
        sa.UniqueConstraint("number", name=op.f("uq_staff_loans_number")),
    )
    op.create_index(
        op.f("ix_staff_loans_employee_id"), "staff_loans", ["employee_id"], unique=False
    )
    op.create_table(
        "payroll_journals",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("loan_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("journal_date", sa.Date(), nullable=False),
        sa.Column("memo", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=8), server_default="queued", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('accrual', 'payment', 'loan')", name=op.f("ck_payroll_journals_kind")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'posted')", name=op.f("ck_payroll_journals_status")
        ),
        sa.CheckConstraint(
            "(run_id IS NULL) <> (loan_id IS NULL)", name=op.f("ck_payroll_journals_source")
        ),
        sa.ForeignKeyConstraint(
            ["loan_id"], ["staff_loans.id"], name=op.f("fk_payroll_journals_loan_id_staff_loans")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["payroll_runs.id"], name=op.f("fk_payroll_journals_run_id_payroll_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_journals")),
    )
    op.create_index(
        op.f("ix_payroll_journals_loan_id"), "payroll_journals", ["loan_id"], unique=False
    )
    op.create_index(
        op.f("ix_payroll_journals_run_id"), "payroll_journals", ["run_id"], unique=False
    )
    op.create_table(
        "payroll_journal_lines",
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.String(length=20), nullable=False),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("debit", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False),
        sa.Column("credit", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(debit = 0) <> (credit = 0)", name=op.f("ck_payroll_journal_lines_one_side")
        ),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f("ck_payroll_journal_lines_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_payroll_journal_lines_cost_centre_id_cost_centres"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"],
            ["payroll_journals.id"],
            name=op.f("fk_payroll_journal_lines_journal_id_payroll_journals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_journal_lines")),
    )
    op.create_index(
        op.f("ix_payroll_journal_lines_journal_id"),
        "payroll_journal_lines",
        ["journal_id"],
        unique=False,
    )
    op.add_column("payslip_lines", sa.Column("loan_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_payslip_lines_loan_id"), "payslip_lines", ["loan_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_payslip_lines_loan_id_staff_loans"),
        "payslip_lines",
        "staff_loans",
        ["loan_id"],
        ["id"],
    )
    # ### end Alembic commands ###
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(
        op.f("fk_payslip_lines_loan_id_staff_loans"), "payslip_lines", type_="foreignkey"
    )
    op.drop_index(op.f("ix_payslip_lines_loan_id"), table_name="payslip_lines")
    op.drop_column("payslip_lines", "loan_id")
    op.drop_index(op.f("ix_payroll_journal_lines_journal_id"), table_name="payroll_journal_lines")
    op.drop_table("payroll_journal_lines")
    op.drop_index(op.f("ix_payroll_journals_run_id"), table_name="payroll_journals")
    op.drop_index(op.f("ix_payroll_journals_loan_id"), table_name="payroll_journals")
    op.drop_table("payroll_journals")
    op.drop_index(op.f("ix_staff_loans_employee_id"), table_name="staff_loans")
    op.drop_table("staff_loans")
    op.drop_table("payroll_banks")
    op.drop_table("payroll_accounts")
    # ### end Alembic commands ###
