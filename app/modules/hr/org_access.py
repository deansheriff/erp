"""Connects the org resolver to the policy engine (ADR 0015): sign-in account →
employee → positions. Wired once at start-up with `authz.engine.set_org_resolver`."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.modules.hr.models import Employee
from app.org import resolver


class PositionTreeResolver:
    def employee_of(self, db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
        return db.scalar(select(Employee.id).where(Employee.user_id == user_id))

    def team_employees(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        employee = self.employee_of(db, user_id)
        if employee is None:
            return set()
        return resolver.reports_of(db, employee, clock.local_date(at))

    def departments_of(self, db: Session, user_id: uuid.UUID, at: datetime) -> set[uuid.UUID]:
        employee = self.employee_of(db, user_id)
        if employee is None:
            return set()
        return resolver.departments_of(db, employee, clock.local_date(at))

    def departments_under(self, db: Session, department_id: uuid.UUID) -> set[uuid.UUID]:
        return resolver.departments_under(db, department_id)

    def department_employees(
        self, db: Session, department_ids: set[uuid.UUID], at: datetime
    ) -> set[uuid.UUID]:
        return resolver.department_employees(db, department_ids, clock.local_date(at))
