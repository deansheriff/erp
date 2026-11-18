"""Helpers for data steps in migrations. Migrations carry their own frozen copies of
seed data rather than importing app code, so old migrations keep working as the code
changes.
"""

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op


def add_permissions(
    rows: Iterable[tuple[str, bool]],
    *,
    grant_to_templates: dict[str, str] | None = None,
) -> None:
    """Insert permission keys (key, sensitive) and grant each to the roles copied from
    the given templates, e.g. {"organization_administrator": "organization"}."""
    conn = op.get_bind()
    for key, sensitive in rows:
        conn.execute(
            sa.text(
                "INSERT INTO permissions (key, module, sensitive) VALUES (:k, :m, :s) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key, "m": key.split(".", 1)[0], "s": sensitive},
        )
        for template_key, scope in (grant_to_templates or {}).items():
            conn.execute(
                sa.text(
                    "INSERT INTO role_grants (role_id, permission_key, scope) "
                    "SELECT id, :k, :scope FROM roles WHERE template_key = :t "
                    "ON CONFLICT DO NOTHING"
                ),
                {"k": key, "scope": scope, "t": template_key},
            )
    bump_authz_version()


def bump_authz_version() -> None:
    op.execute("UPDATE authz_state SET version = version + 1")
