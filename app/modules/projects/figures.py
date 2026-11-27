"""Project figures (ADR 0031): revenue and costs from ledger lines naming the project,
labour from approved hours at each person's hourly cost rate (not posted), the budget by
category, profitability across projects, cost rates, settings and budget alerts."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import NotFound, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.mail import service as mail
from app.modules.finance import service as finance
from app.modules.hr import service as people
from app.modules.projects import common
from app.modules.projects.common import CENT, ZERO
from app.modules.projects.models import (
    CATEGORIES,
    BudgetAlert,
    BudgetLine,
    CostRate,
    Project,
    TimeEntry,
    Timesheet,
)

# Where a cost comes from decides its budget category; labour is approved time.
SOURCE_CATEGORIES = {
    "stock": "materials",  # stock issues
    "payable": "materials",  # bills
    "receivable": "materials",  # cost of stock sold on the project's invoices
    "expenses": "expenses",  # expense claims
}


# ------------------------------------------------------------------ labour


def _rates(db: Session, employee_ids: set[uuid.UUID]) -> dict[uuid.UUID, list[CostRate]]:
    out: dict[uuid.UUID, list[CostRate]] = defaultdict(list)
    if employee_ids:
        for rate in db.scalars(
            select(CostRate)
            .where(CostRate.employee_id.in_(employee_ids))
            .order_by(CostRate.effective_from)
        ):
            out[rate.employee_id].append(rate)
    return out


def rate_on(rates: list[CostRate], day: date) -> Decimal | None:
    found = None
    for rate in rates:
        if rate.effective_from <= day:
            found = rate.hourly
    return found


@dataclass(slots=True)
class Labour:
    hours: Decimal = ZERO
    cost: Decimal = ZERO
    unrated_hours: Decimal = ZERO  # approved hours with no rate in force


def labour(
    db: Session,
    project_ids: set[uuid.UUID],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[uuid.UUID, Labour]:
    """Approved hours and their cost, by project."""
    out: dict[uuid.UUID, Labour] = defaultdict(Labour)
    if not project_ids:
        return out
    query = (
        select(Timesheet.project_id, Timesheet.employee_id, TimeEntry.day, TimeEntry.hours)
        .join(Timesheet, Timesheet.id == TimeEntry.timesheet_id)
        .where(Timesheet.project_id.in_(project_ids), Timesheet.status == "approved")
    )
    if start:
        query = query.where(TimeEntry.day >= start)
    if end:
        query = query.where(TimeEntry.day <= end)
    rows = db.execute(query).all()
    rates = _rates(db, {r[1] for r in rows})
    for project_id, employee_id, day, hours in rows:
        found = out[project_id]
        found.hours += hours
        rate = rate_on(rates.get(employee_id, []), day)
        if rate is None:
            found.unrated_hours += hours
        else:
            found.cost += (hours * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return out


# ------------------------------------------------------------------ figures


@dataclass(slots=True)
class Figures:
    revenue: Decimal = ZERO
    actual: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, ZERO))
    budget: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, ZERO))
    hours: Decimal = ZERO
    unrated_hours: Decimal = ZERO

    @property
    def cost(self) -> Decimal:
        return sum(self.actual.values(), ZERO)

    @property
    def profit(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def margin(self) -> Decimal | None:
        if not self.revenue:
            return None
        return (self.profit * 100 / self.revenue).quantize(Decimal("0.1"))

    @property
    def budget_total(self) -> Decimal:
        return sum(self.budget.values(), ZERO)

    @property
    def used(self) -> int | None:
        """Total cost as a percentage of the total budget."""
        if not self.budget_total:
            return None
        return int((self.cost * 100 / self.budget_total).to_integral_value(ROUND_HALF_UP))

    def used_of(self, category: str) -> int | None:
        if not self.budget[category]:
            return None
        share = self.actual[category] * 100 / self.budget[category]
        return int(share.to_integral_value(ROUND_HALF_UP))


def figures(
    db: Session,
    project_ids: set[uuid.UUID],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[uuid.UUID, Figures]:
    out: dict[uuid.UUID, Figures] = {pid: Figures() for pid in project_ids}
    for total in finance.project_totals(db, project_ids, start=start, end=end):
        found = out[total.project_id]
        if total.account_type == "income":
            found.revenue -= total.net  # income is a credit
        elif total.account_type == "expense":
            found.actual[SOURCE_CATEGORIES.get(total.source, "other")] += total.net
    for project_id, hours in labour(db, project_ids, start=start, end=end).items():
        found = out[project_id]
        found.actual["labour"] += hours.cost
        found.hours, found.unrated_hours = hours.hours, hours.unrated_hours
    for line in db.scalars(select(BudgetLine).where(BudgetLine.project_id.in_(project_ids))):
        out[line.project_id].budget[line.category] = line.amount
    return out


@dataclass(frozen=True, slots=True)
class ProfitRow:
    project: Project
    client: str | None
    figures: Figures


def profitability(
    db: Session,
    actor: Actor,
    *,
    status: str = "active",
    start: date | None = None,
    end: date | None = None,
) -> list[ProfitRow]:
    common.require(db, actor, common.READ)
    query = select(Project).order_by(Project.code)
    if status in common.STATUS_LABELS:
        query = query.where(Project.status == status)
    projects = list(db.scalars(query))
    numbers = figures(db, {p.id for p in projects}, start=start, end=end)
    clients = finance.customer_names(db, {p.customer_id for p in projects if p.customer_id})
    return [
        ProfitRow(p, clients.get(p.customer_id) if p.customer_id else None, numbers[p.id])
        for p in projects
    ]


# ------------------------------------------------------------------ cost rates


@dataclass(frozen=True, slots=True)
class RateRow:
    employee_id: uuid.UUID
    name: str
    current: Decimal | None
    since: date | None
    history: list[CostRate]


def rate_rows(db: Session, actor: Actor) -> list[RateRow]:
    common.require(db, actor, common.RATES)
    ids = people.current_ids(db)
    names = people.names(db, set(ids))
    rates = _rates(db, set(ids))
    today = clock.today()
    out = []
    for employee_id in sorted(ids, key=lambda i: names.get(i, "")):
        history = rates.get(employee_id, [])
        live = [r for r in history if r.effective_from <= today]
        out.append(
            RateRow(
                employee_id,
                names.get(employee_id, ""),
                live[-1].hourly if live else None,
                live[-1].effective_from if live else None,
                list(reversed(history)),
            )
        )
    return out


def add_rate(
    db: Session, actor: Actor, *, employee_id: str, hourly: str, effective_from: date | None
) -> None:
    common.require(db, actor, common.RATES)
    errors: dict[str, str] = {}
    employee = common.uuid_or_none(employee_id)
    if employee is None or employee not in set(people.current_ids(db)):
        errors["employee_id"] = "Choose a person."
    amount = common.parse_money(hourly, "hourly", errors)
    if not hourly.strip() and "hourly" not in errors:
        errors["hourly"] = "Enter the hourly cost."
    if effective_from is None:
        errors["effective_from"] = "Enter the date it applies from."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert employee is not None and effective_from is not None  # noqa: S101
    row = db.scalars(
        select(CostRate).where(
            CostRate.employee_id == employee, CostRate.effective_from == effective_from
        )
    ).first()
    if row is None:
        row = CostRate(employee_id=employee, effective_from=effective_from, hourly=amount)
        db.add(row)
    row.hourly, row.created_by = amount, actor.user_id
    db.flush()
    record_by(
        db,
        actor,
        "projects.rate.set",
        entity_type="employee",
        entity_id=str(employee),
        changes={"from": effective_from.isoformat()},  # the amount itself isn't logged
    )


def save_settings(db: Session, actor: Actor, *, warn_at: str, alert_at: str) -> None:
    common.require(db, actor, common.SETTINGS)
    errors: dict[str, str] = {}
    values = {}
    for key, text in (("warn_at", warn_at), ("alert_at", alert_at)):
        try:
            values[key] = int(text.strip().rstrip("%"))
        except ValueError:
            values[key] = 0
        if not 1 <= values[key] <= 500:
            errors[key] = "Enter a percentage, like 80."
    if not errors and values["alert_at"] <= values["warn_at"]:
        errors["alert_at"] = "The second alert must be higher than the first."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = common.settings(db)
    before = (row.warn_at, row.alert_at)
    row.warn_at, row.alert_at = values["warn_at"], values["alert_at"]
    db.flush()
    record_by(
        db,
        actor,
        "projects.settings.update",
        entity_type="project_settings",
        entity_id="1",
        changes={"thresholds": {"from": before, "to": (row.warn_at, row.alert_at)}},
    )


# ------------------------------------------------------------------ budget alerts


def send_budget_alerts(db: Session, organisation: str) -> int:
    """Email each active project's manager once per threshold its cost passes. Returns
    how many emails were queued."""
    settings = common.settings(db)
    projects = list(db.scalars(select(Project).where(Project.status == "active")))
    numbers = figures(db, {p.id for p in projects})
    sent = {
        (a.project_id, a.threshold)
        for a in db.scalars(
            select(BudgetAlert).where(BudgetAlert.project_id.in_([p.id for p in projects]))
        )
    }
    queued = 0
    for project in projects:
        used = numbers[project.id].used
        due = [t for t in (settings.warn_at, settings.alert_at) if used is not None and used >= t]
        due = [t for t in due if (project.id, t) not in sent]
        if not due:
            continue
        for threshold in due:
            db.add(BudgetAlert(project_id=project.id, threshold=threshold))
        account = people.accounts_of(db, {project.manager_employee_id}).get(
            project.manager_employee_id
        )
        user = db.get(User, account) if account else None
        if user is None or user.status != "active":
            continue
        figures_ = numbers[project.id]
        mail.queue(
            db,
            to=user.email,
            template="projects_budget_alert",
            context={
                "organisation": organisation,
                "name": user.full_name,
                "code": project.code,
                "project": project.name,
                "used": used,
                "threshold": max(due),
                "cost": f"₦{figures_.cost:,.2f}",
                "budget": f"₦{figures_.budget_total:,.2f}",
            },
        )
        queued += 1
    db.flush()
    return queued


def check_found(db: Session, project_id: uuid.UUID) -> Project:
    row = db.get(Project, project_id)
    if row is None:
        raise NotFound()
    return row
