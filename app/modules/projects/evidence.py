"""What projects records about a person over a period, as plain facts for the assistant's
appraisal evidence (ADR 0035): hours logged by project and their tasks by status. The
caller has already checked the person is in the reader's scope."""

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.modules.projects.models import Project, Task, TimeEntry, Timesheet


def facts(db: Session, employee_id: uuid.UUID, start: date, end: date) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    hours = func.sum(TimeEntry.hours)
    for code, name, total in db.execute(
        select(Project.code, Project.name, hours)
        .join(Timesheet, Timesheet.id == TimeEntry.timesheet_id)
        .join(Project, Project.id == Timesheet.project_id)
        .where(
            Timesheet.employee_id == employee_id,
            Timesheet.status.in_(("submitted", "approved")),
            TimeEntry.day >= start,
            TimeEntry.day <= end,
        )
        .group_by(Project.code, Project.name)
        .order_by(hours.desc())
    ).all():
        out.append(("Projects", f"Hours logged on {code} {name}: {float(total):g} hours."))
    counts: dict[str, int] = {
        status: int(n)
        for status, n in db.execute(
            select(Task.status, func.count())
            .where(Task.assignee_employee_id == employee_id)
            .group_by(Task.status)
        ).all()
    }
    if counts:
        assigned = sum(counts.values())
        out.append(
            (
                "Projects",
                f"Project tasks assigned to them now: {assigned}; done {counts.get('done', 0)}, "
                f"in progress {counts.get('in_progress', 0)}, blocked {counts.get('blocked', 0)}, "
                f"to do {counts.get('todo', 0)}.",
            )
        )
        overdue = db.scalar(
            select(func.count()).where(
                Task.assignee_employee_id == employee_id,
                Task.status != "done",
                Task.due_date < clock.today(),
            )
        )
        if overdue:
            out.append(("Projects", f"Tasks past their due date and not done: {overdue}."))
    return out
