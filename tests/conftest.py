"""Shared fixtures.

Database tests run against real PostgreSQL 16 (never SQLite), connecting as kx_app.
Set KX_TEST_DATABASE_URL and KX_TEST_MIGRATOR_DATABASE_URL (see .env.example). In CI a
missing database is a failure; locally, database tests are skipped with a clear reason.
"""

import os
from collections.abc import Iterator

import pytest

# Settings must be in place before anything imports app.core.config.
os.environ["KX_ENV"] = "test"
os.environ.setdefault("KX_SECRET_KEY", "test-secret-key-that-is-long-enough-0001")
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
