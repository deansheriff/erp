"""Projects through the browser's eyes (Phase 11d): the administrator sets up a project
led by an employee, adds a task and a cost rate; the employee logs and submits a week;
with no line manager above them, the administrator steps in to approve it; and every
projects page renders."""

import html
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.projects.models import Project, Task, Timesheet
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _add_and_invite

pytestmark = pytest.mark.db


def _post(client: TestClient, url: str, data: dict[str, Any] | None = None) -> Any:
    return client.post(url, data={**(data or {}), "csrf_token": csrf_token(client)})


def _one(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def test_every_projects_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in (
        "/projects",
        "/projects?tab=all",
        "/projects/new",
        "/projects/timesheet",
        "/projects/approvals",
        "/projects/profitability",
        "/projects/profitability?status=all",
        "/projects/settings",
    ):
        assert admin.get(page).status_code == 200, page
    assert "No HR record yet" in admin.get("/projects/timesheet").text
    nav = admin.get("/projects").text
    for href in ("/projects/timesheet", "/projects/approvals", "/projects/profitability"):
        assert f'href="{href}"' in nav
    r = _post(admin, "/projects/settings/alerts", {"warn_at": "90", "alert_at": "80"})
    assert r.status_code == 422 and "higher than the first" in html.unescape(r.text)
    r = _post(admin, "/projects/settings/alerts", {"warn_at": "75", "alert_at": "100"})
    assert r.status_code == 200 and "Settings saved." in r.text


def test_a_project_from_set_up_to_approved_time(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", {"year": "2026"})
    amaka_id, amaka = _add_and_invite(admin)
    today = clock.today()
    r = _post(
        admin,
        "/projects",
        {"name": "", "manager_id": str(amaka_id), "start_date": today.isoformat()},
    )
    assert r.status_code == 422 and "Name the project." in html.unescape(r.text)
    r = _post(
        admin,
        "/projects",
        {
            "name": "Ikeja warehouse fit-out",
            "manager_id": str(amaka_id),
            "start_date": (today - timedelta(days=30)).isoformat(),
            "budget_labour": "100,000",
            "budget_materials": "250,000",
            "milestone_name": ["Design sign-off"],
            "milestone_due": [today.isoformat()],
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "PRJ-00001" in page and "Design sign-off" in page
    assert "Budget against actual" in page  # the administrator sees the figures
    project = _one(Project, code="PRJ-00001")
    r = _post(admin, f"/projects/{project}/status", {"status": "active"})
    assert "Status changed." in r.text

    assert admin.get(f"/projects/{project}/tasks/new").status_code == 200
    r = _post(
        admin,
        f"/projects/{project}/tasks",
        {"title": "Install racking", "assignee_id": str(amaka_id), "status": "todo"},
    )
    assert r.status_code == 200 and "Install racking" in r.text
    task = _one(Task, title="Install racking")
    assert admin.get(f"/projects/tasks/{task}").status_code == 200
    r = _post(admin, f"/projects/tasks/{task}/status", {"status": "in_progress"})
    assert r.status_code == 200

    r = _post(
        admin,
        "/projects/settings/rate",
        {
            "employee_id": str(amaka_id),
            "hourly": "6,500",
            "effective_from": (today - timedelta(days=60)).isoformat(),
        },
    )
    assert r.status_code == 200 and "₦6,500.00" in html.unescape(r.text)

    week = today - timedelta(days=today.weekday())
    r = _post(
        amaka,
        "/projects/timesheet",
        {"week": week.isoformat(), "rp_0": project, "rt_0": task, "h_0_0": "7.5", "h_0_1": "30"},
    )
    assert r.status_code == 422 and "quarters" in html.unescape(r.text)
    r = _post(
        amaka,
        "/projects/timesheet",
        {"week": week.isoformat(), "rp_0": project, "rt_0": task, "h_0_0": "7.5", "h_0_1": "6"},
    )
    assert r.status_code == 200 and "Hours saved." in r.text
    r = _post(
        amaka, "/projects/timesheet", {"week": week.isoformat(), "save": "0", "submit": project}
    )
    assert r.status_code == 200 and "Sent to someone who can step in." in html.unescape(r.text)

    page = admin.get("/projects/approvals").text
    assert "13.5 h" in page
    sheet = _one(Timesheet, project_id=uuid.UUID(project))
    r = _post(admin, f"/projects/approvals/{sheet}/approve")
    assert r.status_code == 200 and "Approved." in r.text
    page = html.unescape(admin.get(f"/projects/{project}").text)
    assert "₦87,750.00" in page  # 13.5 hours at 6,500
    assert "Ikeja warehouse fit-out" in admin.get("/projects/profitability").text
    mine = amaka.get("/projects?tab=mine").text
    assert "Ikeja warehouse fit-out" in mine
