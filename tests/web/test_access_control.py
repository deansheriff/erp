"""Phase 3 through the browser's eyes: routes check permissions live, denials are audited,
the navigation follows permissions, and the admin screens work end to end."""

import html
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit.models import AuditLog
from app.authz.models import Role
from app.core.db import session_scope
from app.identity.models import User
from tests.conftest import Installed, csrf_token, sign_in
from tests.identity.test_accounts import _accept, _invite

pytestmark = pytest.mark.db


def _role_id(template_key: str) -> uuid.UUID:
    with session_scope() as db:
        return db.scalars(select(Role.id).where(Role.template_key == template_key)).one()


def _user_id(email: str) -> uuid.UUID:
    with session_scope() as db:
        return db.scalars(select(User.id).where(User.email == email)).one()


@pytest.fixture()
def employee(installed: Installed, client: TestClient) -> TestClient:
    """Chinedu: invited by the super admin, signed in, no roles yet. `client` stays
    signed in as the super admin."""
    token = _invite(client, installed)
    return _accept(token, client.app)


def test_people_without_a_role_are_refused_and_the_refusal_is_audited(
    employee: TestClient,
) -> None:
    response = employee.get("/admin/users")
    assert response.status_code == 403
    with session_scope() as db:
        denied = db.scalars(
            select(AuditLog).where(
                AuditLog.outcome == "denied", AuditLog.action == "identity.user.read"
            )
        ).all()
    assert denied and denied[-1].entity_id == "/admin/users"


def test_navigation_only_shows_what_you_may_use(employee: TestClient, client: TestClient) -> None:
    assert "Administration" not in employee.get("/").text
    admin_page = client.get("/admin/users").text
    for href in (
        "/admin/users",
        "/admin/roles",
        "/admin/delegations",
        "/admin/audit",
        "/admin/mail",
    ):
        assert f'href="{href}"' in admin_page, href


def test_giving_a_role_takes_effect_on_the_next_request(
    employee: TestClient, client: TestClient
) -> None:
    chinedu = _user_id("chinedu.eze@adeyemiholdings.ng")
    page = client.get(f"/admin/users/{chinedu}/access")
    assert page.status_code == 200 and "No roles yet" in page.text
    response = client.post(
        f"/admin/users/{chinedu}/access",
        data={
            "role_id": str(_role_id("auditor")),
            "reason": "Year-end audit support",
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 200, response.text[:400]
    assert "Role given" in response.text
    assert employee.get("/admin/audit").status_code == 200
    assert employee.get("/admin/mail").status_code == 403


def test_a_role_needs_a_reason(employee: TestClient, client: TestClient) -> None:
    chinedu = _user_id("chinedu.eze@adeyemiholdings.ng")
    response = client.post(
        f"/admin/users/{chinedu}/access",
        data={"role_id": str(_role_id("auditor")), "reason": "", "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 422
    assert "Say why" in response.text


def test_admins_cannot_change_their_own_roles(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    page = client.get(f"/admin/users/{installed.admin_id}/access")
    assert "Another administrator has to change your roles" in html.unescape(page.text)
    assert "Give a role" not in page.text


def test_roles_can_be_created_and_edited_from_the_matrix(
    installed: Installed, client: TestClient
) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    token = csrf_token(client)
    response = client.post(
        "/admin/roles", data={"name": "Helpdesk", "description": "", "csrf_token": token}
    )
    assert response.status_code == 200
    match = re.search(r"/admin/roles/([0-9a-f-]{36})", str(response.url))
    assert match, response.url
    role_id = match.group(1)
    response = client.post(
        f"/admin/roles/{role_id}",
        data={
            "name": "Helpdesk",
            "description": "First-line account help",
            "scope:identity.user.read": "organization",
            "scope:identity.user.manage": "none",
            "csrf_token": token,
        },
    )
    assert response.status_code == 200, response.text[:400]
    assert "Role saved" in response.text
    with session_scope() as db:
        from app.authz import service as authz

        assert authz.get_role(db, uuid.UUID(role_id)).grants == {
            "identity.user.read": "organization"
        }


def test_the_protected_role_is_read_only(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    page = client.get(f"/admin/roles/{_role_id('super_administrator')}")
    assert page.status_code == 200
    assert "can't be edited or deleted" in html.unescape(page.text)
    assert "Save changes" not in page.text


def test_unknown_records_answer_404(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    assert client.get(f"/admin/roles/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/admin/users/{uuid.uuid4()}/access").status_code == 404


def test_audit_log_lists_and_explains_events(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    page = client.get("/admin/audit?outcome=success")
    assert page.status_code == 200
    assert "setup.complete" in page.text
    match = re.search(r"event=([0-9a-f-]{36})", page.text)
    assert match
    detail = client.get(f"/admin/audit?event={match.group(1)}")
    assert "Entry details" in detail.text


def test_delegations_page_and_lending(employee: TestClient, client: TestClient) -> None:
    chinedu = _user_id("chinedu.eze@adeyemiholdings.ng")
    page = client.get("/delegations")
    assert page.status_code == 200 and "You're lending" in html.unescape(page.text)
    response = client.post(
        "/delegations",
        data={
            "to_user_id": str(chinedu),
            "perm:audit.log.read": "on",
            "scope": "organization",
            "ends": "2099-01-01",
            "reason": "Annual leave handover",
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 422
    assert "at most 90 days" in response.text
