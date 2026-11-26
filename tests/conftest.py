"""Shared fixtures.

Database tests run against real PostgreSQL 16 (never SQLite), connecting as kx_app.
Set KX_TEST_DATABASE_URL and KX_TEST_MIGRATOR_DATABASE_URL (see .env.example). In CI a
missing database is a failure; locally, database tests are skipped with a clear reason.
"""

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

# Settings must be in place before anything imports app.core.config.
os.environ["KX_ENV"] = "test"
os.environ.setdefault("KX_SECRET_KEY", "test-secret-key-that-is-long-enough-0001")
os.environ.setdefault("KX_ENCRYPTION_KEY", "q6cpp1uQ3wJ1L9n0bIYw8Hq9Yw2c5P0b7kqvVf3Q0hQ=")
# Object storage is emulated in-process by moto; files are scanned by a test scanner.
os.environ["KX_S3_ENDPOINT_URL"] = ""
os.environ["KX_S3_ACCESS_KEY"] = "test-access-key"
os.environ["KX_S3_SECRET_KEY"] = "test-secret-key"
os.environ["KX_SCANNER"] = "accept_all"
os.environ["KX_DATABASE_URL"] = os.environ.get(
    "KX_TEST_DATABASE_URL", "postgresql+psycopg://kx_app:unset@127.0.0.1:1/unset"
)

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session

TEST_DB = os.environ.get("KX_TEST_DATABASE_URL")
TEST_MIGRATOR_DB = os.environ.get("KX_TEST_MIGRATOR_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DB and TEST_MIGRATOR_DB:
        return
    if os.environ.get("CI"):
        raise pytest.UsageError(
            "CI requires KX_TEST_DATABASE_URL and KX_TEST_MIGRATOR_DATABASE_URL"
        )
    skip = pytest.mark.skip(reason="no test database configured (KX_TEST_DATABASE_URL)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def _alembic_config() -> Config:
    cfg = Config("alembic.ini")
    cfg.cmd_opts = None
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _runtime() -> None:
    """Wire the app exactly as the entry points do (real org resolver)."""
    from app.wiring import configure_runtime

    configure_runtime()


@pytest.fixture(autouse=True)
def s3() -> Iterator[None]:
    """An in-memory S3 for every test (moto), with both buckets created."""
    from moto import mock_aws

    from app.files import storage

    with mock_aws():
        storage.client.cache_clear()
        storage.ensure_buckets()
        yield
    storage.client.cache_clear()


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[None]:
    """Rebuild the test schema from migrations once per run, as kx_migrator."""
    assert TEST_MIGRATOR_DB
    os.environ["KX_MIGRATOR_DATABASE_URL"] = TEST_MIGRATOR_DB
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(scope="session")
def engine(migrated_db: None) -> Engine:
    from app.core.db import get_engine

    return get_engine()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after the test."""
    with engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            trans.rollback()


# ---------------------------------------------------------------- Phase 2: identity fixtures

IDENTITY_TABLES = (
    "mfa_recovery_codes, user_tokens, user_sessions, api_keys, login_attempts, "
    "file_links, files, role_assignments, delegations, position_assignments, positions, "
    "departments, grades, "
    "branches, cost_centres, employees, users, mail_outbox, mail_settings, organization"
)


def _migration(filename: str) -> ModuleType:
    path = Path(__file__).parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def clean_db(migrated_db: None) -> Iterator[None]:
    """Empty identity data and mark the installation as not set up. Uses the migrator
    (as table owner), because the app role deliberately cannot TRUNCATE."""
    from sqlalchemy import create_engine, text

    from app.identity import service as identity

    assert TEST_MIGRATOR_DB
    owner = create_engine(TEST_MIGRATOR_DB)
    with owner.begin() as conn:
        conn.execute(text("SET ROLE kx_owner"))
        conn.execute(text(f"TRUNCATE {IDENTITY_TABLES} CASCADE"))
        conn.execute(text("DELETE FROM roles WHERE template_key IS NULL"))
        conn.execute(text("UPDATE authz_state SET version = version + 1"))
        conn.execute(text("UPDATE org_state SET version = version + 1"))
        conn.execute(text("UPDATE storage_settings SET used_bytes = 0, quota_bytes = 53687091200"))
        conn.execute(text("UPDATE number_sequences SET next_value = 1"))
        conn.execute(
            text("UPDATE number_sequences SET prefix = 'EMP-', padding = 5 WHERE key = 'employee'")
        )
        conn.execute(
            text(
                "UPDATE expense_settings SET second_level_above = 100000, finance_above = 500000, "
                "weekly_limit = 50000, monthly_limit = 150000"
            )
        )
        # Truncating grades cascades to entitlement rules; restore the seeded defaults.
        conn.execute(
            text(
                "INSERT INTO leave_entitlement_rules (id, leave_type_id, min_service_months, days) "
                "SELECT gen_random_uuid(), t.id, r.m, r.d FROM leave_types t "
                "JOIN (VALUES ('annual', 12, 6), ('sick', 0, 12), ('maternity', 0, 84)) "
                "AS r(c, m, d) ON r.c = t.code"
            )
        )
        # Payroll setup is seeded by its migration; put the defaults back.
        conn.execute(
            text(
                "TRUNCATE pay_groups, pay_components, salary_structures, statutory_sets, "
                "payroll_banks, payroll_accounts, finance_accounts, finance_settings, "
                "fiscal_years, journals, recurring_journals, customers, sales_documents, "
                "receipts, sales_allocations, vat_rates, wht_types, stamp_duty_types, "
                "tax_returns, suppliers, purchase_orders, goods_receipts, bills, "
                "supplier_payments, bank_accounts, bank_rules, bank_transfers, asset_categories, "
                "fixed_assets, asset_changes, depreciation_runs, inventory_units, warehouses, "
                "item_categories, items, stock_moves, stock_documents, stock_counts, "
                "material_requests, low_stock_alerts CASCADE"
            )
        )
        _migration("0010_payroll.py").seed_defaults(conn)
        _migration("0011_payroll_loans_and_remittances.py").seed_defaults(conn)
        _migration("0012_finance_ledger.py").seed_defaults(conn)
        _migration("0013_finance_receivables_and_tax.py").seed_defaults(conn)
        _migration("0015_finance_banking.py").seed_defaults(conn)
        _migration("0016_finance_fixed_assets.py").seed_defaults(conn)
        _migration("0017_inventory.py").seed_defaults(conn)
        conn.execute(
            text(
                "UPDATE installation SET installed_at = NULL, setup_token_hash = NULL, "
                "setup_token_expires_at = NULL"
            )
        )
    owner.dispose()
    identity._installed_cache = False
    yield
    identity._installed_cache = False


SUPER_ADMIN_PASSWORD = "Correct-Horse-Battery-71"  # test-only value
EMPLOYEE_PASSWORD = "Purple-Monsoon-Kettle-42"  # test-only value


class Installed:
    """An installed organisation with a super admin (MFA on) and working mail settings."""

    def __init__(self, admin_id: object, admin_secret: str, recovery_codes: list[str]) -> None:
        self.admin_id = admin_id
        self.admin_email = "fatima.bello@adeyemiholdings.ng"
        self.admin_password = SUPER_ADMIN_PASSWORD
        self.admin_secret = admin_secret
        self.recovery_codes = recovery_codes


@pytest.fixture()
def installed(clean_db: None) -> Installed:
    import pyotp

    from app.core.db import session_scope
    from app.identity import service as identity
    from app.identity.schemas import Actor, AdminAccountInput
    from app.mail import service as mail
    from app.mail.schemas import MailSettingsInput
    from app.org import service as org
    from app.org.schemas import OrganizationInput

    secret = pyotp.random_base32()
    with session_scope() as db:
        org.save_organization(db, OrganizationInput(legal_name="Adeyemi Holdings Limited"))
        admin_id, codes = identity.setup_create_super_admin(
            db,
            AdminAccountInput(
                full_name="Fatima Bello",
                email="fatima.bello@adeyemiholdings.ng",
                password=SUPER_ADMIN_PASSWORD,
                password_confirm=SUPER_ADMIN_PASSWORD,
                mfa_code=pyotp.TOTP(secret).now(),
            ),
            secret,
        )
        mail.save_settings(
            db,
            MailSettingsInput(
                host="127.0.0.1",
                port=1025,
                security="none",
                from_name="Adeyemi Holdings ERP",
                from_address="erp@adeyemiholdings.ng",
            ),
            actor_id=None,
            actor_label="test",
        )
        actor = Actor(
            user_id=admin_id,
            email="fatima.bello@adeyemiholdings.ng",
            full_name="Fatima Bello",
            mfa_enabled=True,
            mfa_required=True,
            mfa_satisfied=True,
            via="session",
        )
        identity.complete_setup(db, actor)
    return Installed(admin_id, secret, codes)


@pytest.fixture()
def client(migrated_db: None) -> Iterator["TestClient"]:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(), base_url="http://localhost:8000") as test_client:
        yield test_client


def csrf_token(client: "TestClient") -> str:
    """The CSRF token rendered into the signed-in shell."""
    import re

    html = client.get("/me/security").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token on the page: is the client signed in?"
    return match.group(1)


def sign_in(
    client: "TestClient", email: str, password: str, totp_secret: str | None = None
) -> None:
    import pyotp

    response = client.post("/login", data={"email": email, "password": password, "next": "/"})
    assert response.status_code == 200, response.text[:500]
    if totp_secret is not None:
        assert response.url.path == "/login/verify"
        token = _form_csrf(response.text)
        response = client.post(
            "/login/verify",
            data={"code": _fresh_code(pyotp.TOTP(totp_secret)), "next": "/", "csrf_token": token},
        )
        assert response.url.path == "/", response.text[:500]


def _form_csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _fresh_code(totp: object) -> str:
    """A code for the next time step, so it is never a replay of one used moments ago."""
    import time

    return totp.at(int(time.time()) + 30)  # type: ignore[attr-defined, no-any-return]


from fastapi.testclient import TestClient  # noqa: E402
