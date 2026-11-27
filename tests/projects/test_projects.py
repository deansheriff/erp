"""Phase 11d: projects set up by a Project Manager and seen by their team; tasks under
milestones; weekly timesheets approved by the manager (or the manager's line manager for
their own time); labour at hourly cost rates; profitability from ledger lines naming the
project; and budget alerts (ADR 0031)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.mail.models import MailOutbox
from app.modules.finance import bills, customers, journals, sales, suppliers
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.customers import CustomerInput
from app.modules.finance.models import JournalLine, WhtType
from app.modules.finance.suppliers import SupplierInput
from app.modules.projects import figures, projects, timesheets
from app.modules.projects.models import Project, Timesheet
from app.modules.projects.projects import TaskInput
from app.modules.projects.timesheets import Cell
from tests.projects.conftest import Work

pytestmark = pytest.mark.db

D = Decimal
WEEK = date(2026, 10, 5)  # a Monday


def _cells(project: uuid.UUID, task: uuid.UUID | None, *hours: tuple[int, str]) -> list[Cell]:
    return [Cell(str(project), str(task or ""), date(2026, 10, 5 + day), h) for day, h in hours]


def _sheet(w: Work, project: uuid.UUID, who: str) -> Timesheet:
    return w.db.scalars(
        select(Timesheet).where(
            Timesheet.project_id == project, Timesheet.employee_id == w.employee(who)
        )
    ).one()


def test_projects_are_set_up_by_their_manager_and_seen_by_their_team(work: Work) -> None:
    w, a = work, work.actor
    with pytest.raises(PermissionDenied):
        projects.save(
            w.db,
            a("Dayo"),
            None,
            projects.ProjectInput("Mine", "", "", None, None, "", "", {}, []),
        )
    row = w.project(start=False)
    project = w.db.get(Project, row)
    assert project is not None and project.code == "PRJ-00001" and project.status == "planned"
    assert projects.get(w.db, a("Dayo"), row).figures is None  # members don't see figures
    assert projects.get(w.db, a("Bola"), row).figures is not None  # Finance does
    with pytest.raises(NotFound):
        projects.get(w.db, a("Hauwa"), row)
    assert [r.project.id for r in projects.list_projects(w.db, a("Dayo"), "mine")] == [row]
    assert projects.list_projects(w.db, a("Hauwa"), "all") == []

    projects.move(w.db, a("Chi"), row, "active")
    with pytest.raises(Conflict):
        projects.move(w.db, a("Chi"), row, "planned")
    with pytest.raises(PermissionDenied):
        projects.move(w.db, a("Dayo"), row, "on_hold")

    milestone = projects.get(w.db, a("Chi"), row).milestones[0].milestone.id
    with pytest.raises(ValidationFailed) as outsider:
        projects.save_task(
            w.db,
            a("Chi"),
            row,
            None,
            TaskInput("Racking", str(milestone), str(w.employee("Hauwa")), None, "todo", ""),
        )
    assert "assignee_id" in (outsider.value.field_errors or {})
    task = projects.save_task(
        w.db,
        a("Chi"),
        row,
        None,
        TaskInput("Racking", str(milestone), str(w.employee("Dayo")), None, "todo", ""),
    )
    projects.set_task_status(w.db, a("Dayo"), task, "in_progress")
    with pytest.raises(PermissionDenied):
        projects.set_task_status(w.db, a("Efe"), task, "done")
    view = projects.get(w.db, a("Dayo"), row)
    assert view.milestones[0].tasks[0].task.status == "in_progress"


def test_timesheets_go_to_the_project_manager(work: Work) -> None:
    w, a = work, work.actor
    row = w.project()
    with pytest.raises(ValidationFailed):
        timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (0, "20"), (0, "6")))
    timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (0, "8"), (1, "4.5")))
    assert timesheets.week_view(w.db, a("Dayo"), WEEK).total == D("12.5")
    timesheets.submit(w.db, a("Dayo"), row, WEEK)
    sheet = _sheet(w, row, "Dayo")
    assert sheet.approver_employee_id == w.employee("Chi")
    with pytest.raises(ValidationFailed):
        timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (2, "1")))  # locked
    with pytest.raises(PermissionDenied):
        timesheets.decide(w.db, a("Ada"), sheet.id, approve=True, note="")  # not the manager
    with pytest.raises(ValidationFailed):
        timesheets.decide(w.db, a("Chi"), sheet.id, approve=False, note="")
    timesheets.decide(w.db, a("Chi"), sheet.id, approve=False, note="Tuesday was a holiday")
    timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (0, "8"), (2, "4")))
    timesheets.submit(w.db, a("Dayo"), row, WEEK)
    assert [p.sheet.id for p in timesheets.pending(w.db, a("Chi"))] == [sheet.id]
    timesheets.decide(w.db, a("Chi"), sheet.id, approve=True, note="")
    assert _sheet(w, row, "Dayo").status == "approved"

    # The manager's own time goes to their line manager.
    timesheets.save_week(w.db, a("Chi"), WEEK, _cells(row, None, (0, "3")))
    timesheets.submit(w.db, a("Chi"), row, WEEK)
    own = _sheet(w, row, "Chi")
    assert own.approver_employee_id == w.employee("Ada")
    with pytest.raises(PermissionDenied):
        timesheets.decide(w.db, a("Chi"), own.id, approve=True, note="")
    with pytest.raises(Conflict, match="waiting for approval"):
        projects.move(w.db, a("Chi"), row, "completed")
    timesheets.decide(w.db, a("Ada"), own.id, approve=True, note="")
    projects.move(w.db, a("Chi"), row, "completed")
    with pytest.raises(ValidationFailed):  # time only on active projects
        timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (3, "2")))


def _customer(w: Work) -> uuid.UUID:
    return customers.save(
        w.db,
        w.actor("Bola"),
        None,
        CustomerInput(
            "Adebayo Foods Ltd", "accounts@adebayo.example", "", "", "", "30", True, True
        ),
    )


def _supplier(w: Work) -> uuid.UUID:
    wht = w.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    return suppliers.save(
        w.db,
        w.actor("Bola"),
        None,
        SupplierInput(
            "Racking Ltd", "company", "", "", "", "", "30", str(wht.id), "", "", "", "", True
        ),
    )


def test_profitability_comes_from_the_ledger_and_approved_time(work: Work) -> None:
    w, a = work, work.actor
    row = w.project(budget={"labour": "100,000", "materials": "250,000"})
    figures.add_rate(
        w.db,
        a("Bola"),
        employee_id=str(w.employee("Dayo")),
        hourly="5,000",
        effective_from=date(2026, 1, 1),
    )
    timesheets.save_week(w.db, a("Dayo"), WEEK, _cells(row, None, (0, "8"), (1, "4")))
    timesheets.submit(w.db, a("Dayo"), row, WEEK)
    timesheets.decide(w.db, a("Chi"), _sheet(w, row, "Dayo").id, approve=True, note="")
    timesheets.save_week(w.db, a("Efe"), WEEK, _cells(row, None, (0, "2")))  # no rate
    timesheets.submit(w.db, a("Efe"), row, WEEK)
    timesheets.decide(w.db, a("Chi"), _sheet(w, row, "Efe").id, approve=True, note="")

    invoice = sales.save_draft(
        w.db,
        a("Bola"),
        None,
        sales.DocumentInput(
            "invoice",
            str(_customer(w)),
            date(2026, 10, 20),
            None,
            None,
            "",
            "",
            "",
            [
                sales.LineInput(
                    "Fit-out stage 1",
                    str(w.account("4100").id),
                    "",
                    "1",
                    "1,000,000",
                    "exempt",
                    project_id=str(row),
                )
            ],
        ),
    )
    sales.issue(w.db, a("Bola"), invoice)
    bill = bills.save_bill(
        w.db,
        a("Bola"),
        None,
        BillInput(
            "bill",
            str(_supplier(w)),
            "RK-77",
            date(2026, 10, 21),
            None,
            "",
            "",
            [
                BillLineInput(
                    "",
                    "Racking",
                    str(w.account("6300").id),
                    "",
                    "1",
                    "300,000",
                    "exempt",
                    project_id=str(row),
                )
            ],
        ),
    )
    bills.approve(w.db, a("Gbenga"), bill)
    draft = journals.save_draft(
        w.db,
        a("Bola"),
        None,
        journal_date=date(2026, 10, 22),
        memo="Site security",
        lines=[
            journals.LineInput(str(w.account("6300").id), "", "", "50,000", "", str(row)),
            journals.LineInput(str(w.account("1010").id), "", "", "", "50,000"),
        ],
    )
    journals.post(w.db, a("Gbenga"), draft)
    tagged = w.db.scalars(select(JournalLine).where(JournalLine.project_id == row)).all()
    assert len(tagged) == 3

    numbers = figures.figures(w.db, {row})[row]
    assert numbers.revenue == D("1000000.00")
    assert numbers.actual == {
        "labour": D("60000.00"),
        "materials": D("300000.00"),
        "expenses": D(0),
        "other": D("50000.00"),
    }
    assert (numbers.hours, numbers.unrated_hours) == (D("14.00"), D("2.00"))
    assert (numbers.profit, numbers.margin, numbers.used) == (D("590000.00"), D("59.0"), 117)
    assert numbers.used_of("materials") == 120

    assert figures.send_budget_alerts(w.db, "Adeyemi Holdings") == 1
    assert figures.send_budget_alerts(w.db, "Adeyemi Holdings") == 0  # once per level
    mail = w.db.scalars(
        select(MailOutbox).where(MailOutbox.template == "projects_budget_alert")
    ).one()
    assert "117%" in mail.subject

    rows = figures.profitability(w.db, a("Bola"))
    assert [r.figures.profit for r in rows] == [D("590000.00")]
    with pytest.raises(PermissionDenied):
        figures.profitability(w.db, a("Chi"))

    projects.move(w.db, a("Chi"), row, "completed")
    with pytest.raises(ValidationFailed):  # costs only on active projects
        journals.save_draft(
            w.db,
            a("Bola"),
            None,
            journal_date=date(2026, 10, 23),
            memo="Late cost",
            lines=[
                journals.LineInput(str(w.account("6300").id), "", "", "1,000", "", str(row)),
                journals.LineInput(str(w.account("1010").id), "", "", "", "1,000"),
            ],
        )


def test_rates_and_settings_are_for_finance(work: Work) -> None:
    w, a = work, work.actor
    with pytest.raises(PermissionDenied):
        figures.add_rate(
            w.db, a("Chi"), employee_id=str(w.employee("Dayo")), hourly="1", effective_from=WEEK
        )
    with pytest.raises(ValidationFailed):
        figures.save_settings(w.db, a("Bola"), warn_at="90", alert_at="80")
    figures.save_settings(w.db, a("Bola"), warn_at="75", alert_at="95")
    rows = figures.rate_rows(w.db, a("Bola"))
    assert any(r.current is None for r in rows)
