"""Invitations, password reset, CSRF, API keys and the interim admin rule."""

import html
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.mail.models import MailOutbox
from tests.conftest import EMPLOYEE_PASSWORD, Installed, csrf_token, sign_in

pytestmark = pytest.mark.db


def _last_mail(template: str) -> MailOutbox:
    with session_scope() as db:
        row = db.scalars(
            select(MailOutbox)
            .where(MailOutbox.template == template)
            .order_by(MailOutbox.created_at.desc())
        ).first()
        assert row is not None
        return row


def _invite(
    client: TestClient, installed: Installed, email: str = "chinedu.eze@adeyemiholdings.ng"
) -> str:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    response = client.post(
        "/admin/users",
        data={"full_name": "Chinedu Eze", "email": email, "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 200, response.text[:300]
    match = re.search(r"/invite/([A-Za-z0-9_-]+)", _last_mail("invite").body_text)
    assert match
    return match.group(1)


def _accept(token: str, app: object) -> TestClient:
    newcomer = TestClient(app, base_url="http://localhost:8000")  # type: ignore[arg-type]
    response = newcomer.post(
        f"/invite/{token}",
        data={
            "full_name": "Chinedu Eze",
            "password": EMPLOYEE_PASSWORD,
            "password_confirm": EMPLOYEE_PASSWORD,
        },
    )
    assert response.url.path == "/", response.text[:300]
    return newcomer


def test_invitation_is_emailed_and_accepted_once(installed: Installed, client: TestClient) -> None:
    token = _invite(client, installed)
    mail = _last_mail("invite")
    assert mail.to_address == "chinedu.eze@adeyemiholdings.ng"
    assert "Fatima Bello has invited you" in mail.body_text
    assert EMPLOYEE_PASSWORD not in mail.body_text

    newcomer = _accept(token, client.app)
    assert "Welcome, Chinedu" in newcomer.get("/").text
    again = TestClient(client.app, base_url="http://localhost:8000")
    assert again.get(f"/invite/{token}").status_code == 404


def test_invitations_expire_after_72_hours(installed: Installed, client: TestClient) -> None:
    token = _invite(client, installed)
    with patch("app.core.clock.now", return_value=clock.now() + timedelta(hours=73)):
        assert client.get(f"/invite/{token}").status_code == 404


def test_resending_an_invitation_invalidates_the_old_link(
    installed: Installed, client: TestClient
) -> None:
    first = _invite(client, installed)
    users = client.get("/admin/users").text
    user_id = re.search(r"/admin/users/([0-9a-f-]{36})/resend", users).group(1)  # type: ignore[union-attr]
    client.post(f"/admin/users/{user_id}/resend", data={"csrf_token": csrf_token(client)})
    assert client.get(f"/invite/{first}").status_code == 404


def test_password_reset_reveals_nothing_and_works_once(
    installed: Installed, client: TestClient
) -> None:
    unknown = client.post("/forgot-password", data={"email": "ghost@adeyemiholdings.ng"})
    known = client.post("/forgot-password", data={"email": installed.admin_email})
    assert "If that address has an account" in unknown.text
    assert unknown.text.count("If that address has an account") == known.text.count(
        "If that address has an account"
    )
    token = re.search(
        r"/reset-password/([A-Za-z0-9_-]+)", _last_mail("password_reset").body_text
    ).group(1)  # type: ignore[union-attr]

    response = client.post(
        f"/reset-password/{token}",
        data={
            "new_password": "Saffron-Lagoon-Tiger-88",
            "confirm_password": "Saffron-Lagoon-Tiger-88",
        },
    )
    assert response.url.path == "/login/verify"  # super admins still need their second factor
    assert client.get(f"/reset-password/{token}").status_code == 404
    with patch("app.core.clock.now", return_value=clock.now() + timedelta(minutes=31)):
        client.post("/forgot-password", data={"email": installed.admin_email})
        late = re.search(
            r"/reset-password/([A-Za-z0-9_-]+)", _last_mail("password_reset").body_text
        ).group(1)  # type: ignore[union-attr]
    with patch("app.core.clock.now", return_value=clock.now() + timedelta(minutes=62)):
        assert client.get(f"/reset-password/{late}").status_code == 404


def test_state_changes_need_the_csrf_token(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    missing = client.post("/me/api-keys", data={"name": "No token"})
    assert missing.status_code == 403
    foreign = client.post(
        "/me/api-keys",
        data={"name": "Cross-site", "csrf_token": csrf_token(client)},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    ok = client.post("/me/api-keys", data={"name": "Ledger sync", "csrf_token": csrf_token(client)})
    assert ok.status_code == 200
    assert "Copy your new key now" in ok.text


def test_api_keys_authenticate_and_stop_when_revoked(
    installed: Installed, client: TestClient
) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    page = client.post(
        "/me/api-keys", data={"name": "Ledger sync", "csrf_token": csrf_token(client)}
    ).text
    key = re.search(r'data-copy="(kx_[^"]+)"', page).group(1)  # type: ignore[union-attr]
    machine = TestClient(client.app, base_url="http://localhost:8000")
    me = machine.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert me.status_code == 200
    assert me.json()["authenticated_with"] == "api_key"
    assert (
        machine.get("/api/v1/me", headers={"Authorization": "Bearer kx_bad_key"}).status_code == 401
    )
    # Keys only open the API, never the web pages.
    assert (
        machine.get(
            "/admin/users", headers={"Authorization": f"Bearer {key}"}, follow_redirects=False
        ).status_code
        == 303
    )

    key_id = re.search(
        r"/me/api-keys/([0-9a-f-]{36})/revoke", client.get("/me/security").text
    ).group(1)  # type: ignore[union-attr]
    client.post(f"/me/api-keys/{key_id}/revoke", data={"csrf_token": csrf_token(client)})
    assert machine.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_employees_cannot_use_admin_pages(installed: Installed, client: TestClient) -> None:
    newcomer = _accept(_invite(client, installed), client.app)
    for path in ("/admin/users", "/admin/mail"):
        response = newcomer.get(path)
        assert response.status_code == 403
        assert "You don't have access to this page" in html.unescape(response.text)
    assert "Administration" not in newcomer.get("/").text


def test_suspending_a_user_ends_their_sessions_and_keys(
    installed: Installed, client: TestClient
) -> None:
    newcomer = _accept(_invite(client, installed), client.app)
    page = newcomer.post(
        "/me/api-keys", data={"name": "Script", "csrf_token": csrf_token(newcomer)}
    ).text
    key = re.search(r'data-copy="(kx_[^"]+)"', page).group(1)  # type: ignore[union-attr]

    users = client.get("/admin/users").text
    user_id = re.search(r"/admin/users/([0-9a-f-]{36})/suspend", users).group(1)  # type: ignore[union-attr]
    client.post(f"/admin/users/{user_id}/suspend", data={"csrf_token": csrf_token(client)})

    assert newcomer.get("/", follow_redirects=False).headers["location"].startswith("/login")
    machine = TestClient(client.app, base_url="http://localhost:8000")
    assert machine.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    blocked = newcomer.post(
        "/login", data={"email": "chinedu.eze@adeyemiholdings.ng", "password": EMPLOYEE_PASSWORD}
    )
    assert blocked.status_code == 422


def test_the_last_super_admin_cannot_be_suspended_by_anyone(
    installed: Installed, client: TestClient
) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    response = client.post(
        f"/admin/users/{installed.admin_id}/suspend", data={"csrf_token": csrf_token(client)}
    )
    assert response.status_code == 409


def test_changing_password_signs_out_other_devices(
    installed: Installed, client: TestClient
) -> None:
    newcomer = _accept(_invite(client, installed), client.app)
    laptop = TestClient(client.app, base_url="http://localhost:8000")
    sign_in(laptop, "chinedu.eze@adeyemiholdings.ng", EMPLOYEE_PASSWORD)
    response = newcomer.post(
        "/me/security/password",
        data={
            "current_password": EMPLOYEE_PASSWORD,
            "new_password": "Indigo-Harbour-Falcon-63",
            "confirm_password": "Indigo-Harbour-Falcon-63",
            "csrf_token": csrf_token(newcomer),
        },
    )
    assert "signed out everywhere else" in response.text
    assert laptop.get("/", follow_redirects=False).status_code == 303
    assert newcomer.get("/", follow_redirects=False).status_code == 200


def test_secrets_never_appear_in_the_audit_log(installed: Installed, client: TestClient) -> None:
    from app.audit.models import AuditLog

    _accept(_invite(client, installed), client.app)
    with session_scope() as db:
        rows = db.scalars(select(AuditLog)).all()
    blob = " ".join(str(r.changes) + str(r.reason) + str(r.actor_label) for r in rows)
    for secret in (EMPLOYEE_PASSWORD, installed.admin_password, installed.admin_secret):
        assert secret not in blob
