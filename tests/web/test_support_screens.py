"""Support through the browser's eyes (Phase 11e): the administrator sets up a team and a
category, an employee raises a ticket, the administrator replies and adds an internal
note, resolves it, the employee rates it, and every support page renders."""

import html
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.support.models import SupportTeam, Ticket, TicketCategory
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


def test_every_support_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in (
        "/support",
        "/support?tab=queue",
        "/support?tab=all",
        "/support/new",
        "/support/report",
        "/support/report?month=2026-10",
        "/support/settings",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/support").text
    for href in ("/support/report", "/support/settings"):
        assert f'href="{href}"' in nav
    r = _post(admin, "/support/settings/hours", {"day_start": "17:00", "day_end": "08:00"})
    assert r.status_code == 422 and "end after it starts" in html.unescape(r.text)


def test_a_ticket_from_raised_to_rated(admin: TestClient) -> None:  # noqa: F811
    r = _post(admin, "/support/settings/team", {"team_name": "IT", "active": "1"})
    assert r.status_code == 200 and "Saved." in r.text
    team = _one(SupportTeam, name="IT")
    r = _post(admin, "/support/settings/category", {"category_name": "Network", "team_id": team})
    assert r.status_code == 200 and "Network" in r.text
    _, amaka = _add_and_invite(admin)

    r = _post(amaka, "/support/new", {"category_id": "", "priority": "high", "subject": ""})
    assert r.status_code == 422 and "Choose what it's about." in html.unescape(r.text)
    r = _post(
        amaka,
        "/support/new",
        {
            "category_id": _one(TicketCategory, name="Network"),
            "priority": "high",
            "subject": "VPN keeps dropping",
            "description": "Every ten minutes since Monday.",
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Ticket raised." in page and "TKT-00001" in page
    ticket = _one(Ticket, number="TKT-00001")
    assert "VPN keeps dropping" in amaka.get("/support?tab=mine").text
    assert "VPN keeps dropping" in admin.get("/support?tab=queue").text

    _post(admin, f"/support/{ticket}/reply", {"kind": "note", "body": "Router logs look odd."})
    r = _post(admin, f"/support/{ticket}/reply", {"kind": "reply", "body": "Try the new client."})
    assert "Sent." in r.text
    mine = html.unescape(amaka.get(f"/support/{ticket}").text)
    assert "Try the new client." in mine and "Router logs look odd." not in mine
    r = _post(admin, f"/support/{ticket}/resolve", {"resolution": ""})
    assert r.status_code == 422 and "Say how it was resolved." in html.unescape(r.text)
    r = _post(admin, f"/support/{ticket}/resolve", {"resolution": "Updated the VPN client."})
    assert "Resolved." in html.unescape(r.text)
    r = _post(amaka, f"/support/{ticket}/rate", {"score": "4", "comment": "Sorted quickly"})
    assert "Thanks for rating it." in r.text
    report = admin.get("/support/report").text
    assert "IT" in report and "Updated" not in report
