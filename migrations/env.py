"""Alembic environment. Migrations run as kx_migrator and create objects as kx_owner."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.db import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "KX_MIGRATOR_DATABASE_URL"
    )
    if not url:
        raise RuntimeError("Set KX_MIGRATOR_DATABASE_URL to run migrations.")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Objects belong to kx_owner, which no runtime process connects as (ADR 0007).
        connection.exec_driver_sql("SET ROLE kx_owner")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
