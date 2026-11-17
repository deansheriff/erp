"""Database engine, sessions and the declarative base (ADR 0004, ADR 0007).

Routes get a session from `get_db` (one transaction per request). Workers and scripts
use `session_scope()`. Raw SQL is allowed in this module and in migrations only.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

from sqlalchemy import DateTime, Engine, MetaData, Uuid, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings
from app.core.ids import new_id

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012  (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(),
    }


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=True)


def get_db() -> Iterator[Session]:
    """Request-scoped session: commits if the handler returns, rolls back if it raises."""
    with get_sessionmaker()() as session, session.begin():
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """A unit of work outside a request (workers, CLI, independent audit writes)."""
    with get_sessionmaker()() as session, session.begin():
        yield session


class UnsafeDatabaseRole(RuntimeError):
    pass


def verify_database_role(engine: Engine) -> None:
    """Refuse to run as a superuser, a role-creating role, or a table owner (ADR 0007)."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        if any(row):
            raise UnsafeDatabaseRole(
                "The application database role must not be a superuser or able to create "
                "roles or databases. Connect as kx_app."
            )
        owned = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tableowner = current_user"
            )
        ).scalar_one()
        if owned:
            raise UnsafeDatabaseRole(
                "The application database role owns tables. Tables must be owned by kx_owner."
            )


def check_database(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
