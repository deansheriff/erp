"""HR record: classed employee fields, change requests, documents, checklists, exits and
employee numbers (ADR 0017).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19 10:00:27.551153
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.helpers import add_permissions, bump_authz_version

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the Phase 6 catalogue additions: (key, sensitive).
CLASS_PERMISSIONS = [
    ("hr.employee.personal.read", False),
    ("hr.employee.personal.update", False),
    ("hr.employee.statutory.read", True),
    ("hr.employee.statutory.update", True),
    ("hr.employee.banking.read", True),
    ("hr.employee.banking.update", True),
    ("hr.employee.compensation.read", True),
    ("hr.employee.compensation.update", True),
    ("hr.employee.sensitive.read", True),
    ("hr.employee.sensitive.update", True),
]
PROCESS_PERMISSIONS = [
    ("hr.checklist.manage", False),
    ("hr.checklist.it", False),
    ("hr.exit.manage", True),
    ("hr.settings.manage", False),
]
SELF_PERMISSIONS = [("hr.profile.request_change", False)]

# Frozen copy of the template grants (ADR 0017): template -> {permission: scope}.
ORG, SELF, TEAM = "organization", "self", "team"
HR_OFFICER = {
    "hr.employee.read": ORG,
    "hr.employee.create": ORG,
    "hr.employee.update": ORG,
    "hr.employee.personal.read": ORG,
    "hr.employee.personal.update": ORG,
    "hr.employee.statutory.read": ORG,
    "hr.employee.statutory.update": ORG,
    "hr.employee.banking.read": ORG,
    "hr.employee.banking.update": ORG,
    "hr.checklist.manage": ORG,
    "hr.exit.manage": ORG,
    "identity.user.invite": ORG,
    "org.structure.read": ORG,
}
TEMPLATE_GRANTS = {
    "employee": {
        "hr.employee.read": ORG,
        "hr.employee.personal.read": SELF,
        "hr.employee.personal.update": SELF,
        "hr.employee.statutory.read": SELF,
        "hr.employee.banking.read": SELF,
        "hr.employee.compensation.read": SELF,
        "hr.employee.sensitive.read": SELF,
        "hr.profile.request_change": SELF,
    },
    "line_manager": {"hr.employee.personal.read": TEAM},
    "hr_officer": HR_OFFICER,
    "hr_manager": {
        **dict.fromkeys(
            ["hr.employee.read", "hr.employee.create", "hr.employee.update"]
            + [k for k, _ in CLASS_PERMISSIONS + PROCESS_PERMISSIONS],
            ORG,
        ),
        "identity.user.invite": ORG,
        "org.structure.read": ORG,
    },
    "it_support": {"hr.employee.read": ORG, "hr.checklist.it": ORG},
}
IT_SUPPORT = (
    "it_support",
    "IT Support",
    "Completes the IT tasks on onboarding and offboarding checklists.",
)

# Seeded, editable defaults (confirm with the product owner).
CATEGORIES = [
    ("Employment contract", "compensation"),
    ("Identity document", "statutory"),
    ("Certificate", "personal"),
    ("Appraisal letter", "personal"),
    ("Medical report", "sensitive"),
]
# (kind, name, [(title, assignee, due offset in days)])
TEMPLATES = [
    (
        "onboarding",
        "Standard onboarding",
        [
            ("Collect the signed offer letter and contract", "hr", 0),
            ("Collect ID, certificates and guarantor forms", "hr", 0),
            ("Set up work email, laptop and system access", "it", -1),
            ("Plan first-week goals and introduce the team", "manager", 0),
            ("Check and complete your profile", "employee", 3),
            ("Add statutory and bank details", "hr", 5),
            ("Book the probation review", "hr", 30),
        ],
    ),
    (
        "offboarding",
        "Standard offboarding",
        [
            ("Hand over work in progress", "manager", -5),
            ("Hold the exit interview", "hr", -3),
            ("Tell payroll about the final pay", "hr", -3),
            ("Return laptop, ID card and other company property", "employee", 0),
            ("Recover devices and remove shared access", "it", 0),
        ],
    ),
]


def _grant(template_key: str, key: str, scope: str) -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO role_grants (role_id, permission_key, scope) "
            "SELECT id, :k, :s FROM roles WHERE template_key = :t ON CONFLICT DO NOTHING"
        ),
        {"k": key, "s": scope, "t": template_key},
    )


def _seed() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO roles (id, name, description, template_key, requires_mfa, protected, "
            "grants_all) VALUES (gen_random_uuid(), :n, :d, :k, false, false, false) "
            "ON CONFLICT DO NOTHING"
        ),
        {"k": IT_SUPPORT[0], "n": IT_SUPPORT[1], "d": IT_SUPPORT[2]},
    )
    add_permissions(
        CLASS_PERMISSIONS + PROCESS_PERMISSIONS,
        grant_to_templates={"organization_administrator": ORG},
    )
    add_permissions(SELF_PERMISSIONS, grant_to_templates={"organization_administrator": SELF})
    for key, _ in CLASS_PERMISSIONS:
        if key.endswith(".read"):
            _grant("auditor", key, ORG)
    for template_key, grants in TEMPLATE_GRANTS.items():
        for key, scope in grants.items():
            _grant(template_key, key, scope)
    bump_authz_version()

    # Employee numbers: existing people are numbered in the order they were added.
    conn.execute(
        sa.text(
            "UPDATE employees e SET employee_number = 'EMP-' || lpad(n.rn::text, 5, '0') "
            "FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn "
            "FROM employees) n WHERE e.id = n.id"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO number_sequences (key, prefix, padding, next_value) "
            "SELECT 'employee', 'EMP-', 5, count(*) + 1 FROM employees"
        )
    )
    for name, field_class in CATEGORIES:
        conn.execute(
            sa.text(
                "INSERT INTO employee_document_categories (id, name, field_class) "
                "VALUES (gen_random_uuid(), :n, :c)"
            ),
            {"n": name, "c": field_class},
        )
    for kind, name, tasks in TEMPLATES:
        template_id = conn.execute(
            sa.text(
                "INSERT INTO checklist_templates (id, kind, name) "
                "VALUES (gen_random_uuid(), :k, :n) RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
        for order, (title, assignee, offset) in enumerate(tasks):
            conn.execute(
                sa.text(
                    "INSERT INTO checklist_template_tasks "
                    "(id, template_id, title, assignee, due_offset_days, sort_order) "
                    "VALUES (gen_random_uuid(), :t, :title, :a, :o, :s)"
                ),
                {"t": template_id, "title": title, "a": assignee, "o": offset, "s": order},
            )


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "employee_document_categories",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("field_class", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "field_class IN ('personal', 'statutory', 'banking', 'compensation', 'sensitive')",
            name=op.f("ck_employee_document_categories_field_class"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_document_categories")),
        sa.UniqueConstraint("name", name=op.f("uq_employee_document_categories_name")),
    )
    op.create_table(
        "number_sequences",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("prefix", sa.String(length=20), server_default="", nullable=False),
        sa.Column("padding", sa.Integer(), server_default="5", nullable=False),
        sa.Column("next_value", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("next_value >= 1", name=op.f("ck_number_sequences_next_value")),
        sa.CheckConstraint("padding BETWEEN 1 AND 12", name=op.f("ck_number_sequences_padding")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_number_sequences")),
    )
    op.create_table(
        "employee_change_requests",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("field_class", sa.String(length=12), nullable=False),
        sa.Column("field_names", sa.Text(), nullable=False),
        sa.Column("values_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "field_class IN ('statutory', 'banking')",
            name=op.f("ck_employee_change_requests_field_class"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_employee_change_requests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_employee_change_requests_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_employee_change_requests_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_employee_change_requests_requested_by_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_change_requests")),
    )
    op.create_index(
        op.f("ix_employee_change_requests_employee_id"),
        "employee_change_requests",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        "uq_change_requests_open",
        "employee_change_requests",
        ["employee_id", "field_class"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "employee_documents",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("field_class", sa.String(length=16), nullable=False),
        sa.Column("version_group_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["added_by"], ["users.id"], name=op.f("fk_employee_documents_added_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["employee_document_categories.id"],
            name=op.f("fk_employee_documents_category_id_employee_document_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_employee_documents_employee_id_employees"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_documents")),
        sa.UniqueConstraint(
            "version_group_id", name=op.f("uq_employee_documents_version_group_id")
        ),
    )
    op.create_index(
        op.f("ix_employee_documents_employee_id"),
        "employee_documents",
        ["employee_id"],
        unique=False,
    )
    op.create_table(
        "employee_exits",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("notice_date", sa.Date(), nullable=False),
        sa.Column("last_working_day", sa.Date(), nullable=False),
        sa.Column("final_pay_due", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("status", sa.String(length=10), server_default="scheduled", nullable=False),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('resignation', 'termination')", name=op.f("ck_employee_exits_kind")
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'revoked', 'cancelled')", name=op.f("ck_employee_exits_status")
        ),
        sa.CheckConstraint("notice_date <= last_working_day", name=op.f("ck_employee_exits_dates")),
        sa.ForeignKeyConstraint(
            ["cancelled_by"], ["users.id"], name=op.f("fk_employee_exits_cancelled_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_employee_exits_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"], ["users.id"], name=op.f("fk_employee_exits_recorded_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_exits")),
    )
    op.create_index(
        op.f("ix_employee_exits_employee_id"), "employee_exits", ["employee_id"], unique=False
    )
    op.create_index(
        "uq_employee_exits_live",
        "employee_exits",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_table(
        "checklist_templates",
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('onboarding', 'offboarding')", name=op.f("ck_checklist_templates_kind")
        ),
        sa.CheckConstraint(
            "department_id IS NULL OR position_id IS NULL",
            name=op.f("ck_checklist_templates_one_target"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_checklist_templates_department_id_departments"),
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
            name=op.f("fk_checklist_templates_position_id_positions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_templates")),
    )
    op.create_table(
        "checklist_template_tasks",
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("assignee", sa.String(length=10), nullable=False),
        sa.Column("due_offset_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "assignee IN ('hr', 'it', 'manager', 'employee')",
            name=op.f("ck_checklist_template_tasks_assignee"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["checklist_templates.id"],
            name=op.f("fk_checklist_template_tasks_template_id_checklist_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_template_tasks")),
    )
    op.create_index(
        op.f("ix_checklist_template_tasks_template_id"),
        "checklist_template_tasks",
        ["template_id"],
        unique=False,
    )
    op.create_table(
        "checklists",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("exit_id", sa.Uuid(), nullable=True),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("manager_employee_id", sa.Uuid(), nullable=True),
        sa.Column("started_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('onboarding', 'offboarding')", name=op.f("ck_checklists_kind")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_checklists_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["exit_id"], ["employee_exits.id"], name=op.f("fk_checklists_exit_id_employee_exits")
        ),
        sa.ForeignKeyConstraint(
            ["manager_employee_id"],
            ["employees.id"],
            name=op.f("fk_checklists_manager_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["started_by"], ["users.id"], name=op.f("fk_checklists_started_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["checklist_templates.id"],
            name=op.f("fk_checklists_template_id_checklist_templates"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklists")),
    )
    op.create_index(op.f("ix_checklists_employee_id"), "checklists", ["employee_id"], unique=False)
    op.create_table(
        "checklist_tasks",
        sa.Column("checklist_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("assignee", sa.String(length=10), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "assignee IN ('hr', 'it', 'manager', 'employee')",
            name=op.f("ck_checklist_tasks_assignee"),
        ),
        sa.ForeignKeyConstraint(
            ["checklist_id"],
            ["checklists.id"],
            name=op.f("fk_checklist_tasks_checklist_id_checklists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["done_by"], ["users.id"], name=op.f("fk_checklist_tasks_done_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_tasks")),
    )
    op.create_index(
        op.f("ix_checklist_tasks_checklist_id"), "checklist_tasks", ["checklist_id"], unique=False
    )
    op.add_column("employees", sa.Column("employee_number", sa.String(length=30), nullable=True))
    op.add_column("employees", sa.Column("branch_id", sa.Uuid(), nullable=True))
    op.add_column("employees", sa.Column("phone", sa.String(length=30), nullable=True))
    op.add_column("employees", sa.Column("personal_email", sa.String(length=254), nullable=True))
    op.add_column("employees", sa.Column("residential_address", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("state_of_residence", sa.String(length=40), nullable=True))
    op.add_column("employees", sa.Column("marital_status", sa.String(length=12), nullable=True))
    op.add_column("employees", sa.Column("next_of_kin_name", sa.String(length=160), nullable=True))
    op.add_column(
        "employees", sa.Column("next_of_kin_relationship", sa.String(length=60), nullable=True)
    )
    op.add_column("employees", sa.Column("next_of_kin_phone", sa.String(length=30), nullable=True))
    op.add_column("employees", sa.Column("next_of_kin_address", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("gender", sa.String(length=10), nullable=True))
    op.add_column("employees", sa.Column("state_of_origin", sa.String(length=40), nullable=True))
    op.add_column("employees", sa.Column("employment_type", sa.String(length=12), nullable=True))
    op.add_column("employees", sa.Column("nysc_start", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("nysc_end", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("hire_date", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("probation_end", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("confirmation_date", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("nin_encrypted", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("tin_encrypted", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("pension_pfa", sa.String(length=120), nullable=True))
    op.add_column("employees", sa.Column("rsa_pin_encrypted", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("nhf_number_encrypted", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("bank_name", sa.String(length=120), nullable=True))
    op.add_column("employees", sa.Column("account_number_encrypted", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("account_name", sa.String(length=160), nullable=True))
    op.add_column("employees", sa.Column("bvn_encrypted", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_employees_employee_number"), "employees", ["employee_number"]
    )
    op.create_foreign_key(
        op.f("fk_employees_branch_id_branches"), "employees", "branches", ["branch_id"], ["id"]
    )
    # ### end Alembic commands ###
    for name, condition in (
        ("employment_type", "employment_type IN ('permanent', 'contract', 'intern', 'nysc')"),
        ("gender", "gender IN ('female', 'male')"),
        (
            "marital_status",
            "marital_status IN ('single', 'married', 'divorced', 'widowed', 'separated')",
        ),
        ("nysc_dates", "nysc_end IS NULL OR nysc_start <= nysc_end"),
    ):
        op.create_check_constraint(op.f(f"ck_employees_{name}"), "employees", condition)
    _seed()


def downgrade() -> None:
    _unseed()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f("fk_employees_branch_id_branches"), "employees", type_="foreignkey")
    op.drop_constraint(op.f("uq_employees_employee_number"), "employees", type_="unique")
    for column in EMPLOYEE_COLUMNS:
        op.drop_column("employees", column)
    _drop_tables()


EMPLOYEE_COLUMNS = [
    "bvn_encrypted",
    "account_name",
    "account_number_encrypted",
    "bank_name",
    "nhf_number_encrypted",
    "rsa_pin_encrypted",
    "pension_pfa",
    "tin_encrypted",
    "nin_encrypted",
    "confirmation_date",
    "probation_end",
    "hire_date",
    "nysc_end",
    "nysc_start",
    "employment_type",
    "state_of_origin",
    "gender",
    "date_of_birth",
    "next_of_kin_address",
    "next_of_kin_phone",
    "next_of_kin_relationship",
    "next_of_kin_name",
    "marital_status",
    "state_of_residence",
    "residential_address",
    "personal_email",
    "phone",
    "branch_id",
    "employee_number",
]


def _unseed() -> None:
    keys = [k for k, _ in CLASS_PERMISSIONS + PROCESS_PERMISSIONS + SELF_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM delegations WHERE permission_key = ANY(:keys)").bindparams(keys=keys)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)").bindparams(keys=keys))
    # Phase 6 grants of earlier permissions to the HR templates.
    for template_key, grants in TEMPLATE_GRANTS.items():
        op.execute(
            sa.text(
                "DELETE FROM role_grants g USING roles r WHERE g.role_id = r.id "
                "AND r.template_key = :t AND g.permission_key = ANY(:keys)"
            ).bindparams(t=template_key, keys=list(grants))
        )
    op.execute(
        sa.text(
            "DELETE FROM role_assignments a USING roles r "
            "WHERE a.role_id = r.id AND r.template_key = 'it_support'"
        )
    )
    op.execute(sa.text("DELETE FROM roles WHERE template_key = 'it_support'"))
    bump_authz_version()
    for name in ("employment_type", "gender", "marital_status", "nysc_dates"):
        op.drop_constraint(op.f(f"ck_employees_{name}"), "employees", type_="check")


def _drop_tables() -> None:
    op.drop_index(op.f("ix_checklist_tasks_checklist_id"), table_name="checklist_tasks")
    op.drop_table("checklist_tasks")
    op.drop_index(op.f("ix_checklists_employee_id"), table_name="checklists")
    op.drop_table("checklists")
    op.drop_index(
        op.f("ix_checklist_template_tasks_template_id"), table_name="checklist_template_tasks"
    )
    op.drop_table("checklist_template_tasks")
    op.drop_table("checklist_templates")
    op.drop_index(
        "uq_employee_exits_live",
        table_name="employee_exits",
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.drop_index(op.f("ix_employee_exits_employee_id"), table_name="employee_exits")
    op.drop_table("employee_exits")
    op.drop_index(op.f("ix_employee_documents_employee_id"), table_name="employee_documents")
    op.drop_table("employee_documents")
    op.drop_index(
        "uq_change_requests_open",
        table_name="employee_change_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        op.f("ix_employee_change_requests_employee_id"), table_name="employee_change_requests"
    )
    op.drop_table("employee_change_requests")
    op.drop_table("number_sequences")
    op.drop_table("employee_document_categories")
    # ### end Alembic commands ###
