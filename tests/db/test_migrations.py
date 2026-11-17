"""Models and migrations agree, so `alembic revision --autogenerate` would produce nothing."""

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

import app.models  # noqa: F401
from app.core.db import Base

pytestmark = pytest.mark.db


def test_models_match_the_migrated_schema(migrated_db: None) -> None:
    engine = create_engine(os.environ["KX_TEST_MIGRATOR_DATABASE_URL"])
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []
