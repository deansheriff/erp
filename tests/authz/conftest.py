"""Helpers for authorisation tests: people and role assignments made directly in the
database, inside the rolled-back `db` session."""

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.models import Role, RoleAssignment
from app.core import clock
from app.identity.models import User
from app.identity.schemas import Actor


class People:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, name: str, *, status: str = "active") -> User:
        user = User(
            email=f"{name.lower().replace(' ', '.')}@adeyemiholdings.ng",
            full_name=name,
            status=status,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def role(self, template_key: str) -> Role:
        return self.db.scalars(select(Role).where(Role.template_key == template_key)).one()

    def give(
        self,
        user: User,
        template_key: str,
        *,
        starts: datetime | None = None,
        ends: datetime | None = None,
        department_id: uuid.UUID | None = None,
    ) -> RoleAssignment:
        assignment = RoleAssignment(
            user_id=user.id,
            role_id=self.role(template_key).id,
            valid_from=starts or clock.now() - timedelta(minutes=1),
            valid_to=ends,
            reason="test fixture",
            department_id=department_id,
        )
        self.db.add(assignment)
        self.db.flush()
        engine.bump_version(self.db)
        return assignment

    @staticmethod
    def actor(user: User) -> Actor:
        return Actor(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            mfa_enabled=True,
            mfa_required=True,
            mfa_satisfied=True,
            via="session",
        )


@pytest.fixture()
def people(clean_db: None, db: Session) -> Iterator[People]:
    yield People(db)
    from app.wiring import configure_runtime

    configure_runtime()  # tests may swap in a fake resolver
