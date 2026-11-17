"""The application connects with a role that cannot change the schema (ADR 0007)."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.core.db import verify_database_role

pytestmark = pytest.mark.db


def test_app_role_passes_the_startup_safety_check(engine: Engine) -> None:
    verify_database_role(engine)


def test_app_role_is_kx_app_and_not_privileged(engine: Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    assert row[0] == "kx_app"
    assert row[1:] == (False, False, False, False)


def test_every_table_is_owned_by_kx_owner(engine: Engine) -> None:
    with engine.connect() as conn:
        owners = set(
            conn.execute(
                text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert owners == {"kx_owner"}


def test_app_role_cannot_create_tables(engine: Engine) -> None:
    with engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
        conn.execute(text("CREATE TABLE should_not_exist (id int)"))
