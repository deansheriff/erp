"""Sign-in, lockout, two-step verification and sessions (docs/IDENTITY.md section 3)."""

import html
from datetime import timedelta
from unittest.mock import patch

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.audit.models import AuditLog
from app.core import clock
from app.core.db import session_scope
from tests.conftest import Installed, _form_csrf, _fresh_code, sign_in

pytestmark = pytest.mark.db


def test_wrong_password_shows_attempts_left_and_is_audited(
    installed: Installed, client: TestClient
) -> None:
    response = client.post("/login", data={"email": installed.admin_email, "password": "nope"})
    assert response.status_code == 422
    assert "4 attempts left" in response.text
    with session_scope() as db:
        denied = db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "identity.login", AuditLog.outcome == "denied")
        )
    assert denied and denied >= 1  # written even though the request "failed"


def test_unknown_email_gets_the_same_message(installed: Installed, client: TestClient) -> None:
    response = client.post("/login", data={"email": "nobody@adeyemiholdings.ng", "password": "x"})
    assert response.status_code == 422
    assert "That email and password don't match." in html.unescape(response.text)
    assert "attempts left" not in response.text  # nothing reveals whether the account exists


def test_five_failures_lock_the_account_even_for_the_right_password(
    installed: Installed, client: TestClient
) -> None:
    for _ in range(5):
        client.post("/login", data={"email": installed.admin_email, "password": "wrong"})
    response = client.post(
        "/login", data={"email": installed.admin_email, "password": installed.admin_password}
    )
    assert response.status_code == 422
    assert "locked" in response.text
    # It unlocks by itself after 15 minutes.
    with patch("app.core.clock.now", return_value=clock.now() + timedelta(minutes=16)):
        response = client.post(
            "/login", data={"email": installed.admin_email, "password": installed.admin_password}
        )
    assert response.url.path == "/login/verify"


def test_super_admin_must_finish_two_step_before_anything_else(
    installed: Installed, client: TestClient
) -> None:
    response = client.post(
        "/login", data={"email": installed.admin_email, "password": installed.admin_password}
    )
    assert response.url.path == "/login/verify"
    blocked = client.get("/admin/users", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"].startswith("/login/verify")

    csrf = _form_csrf(response.text)
    bad = client.post("/login/verify", data={"code": "123456", "next": "/", "csrf_token": csrf})
    assert bad.status_code == 422
    code = _fresh_code(pyotp.TOTP(installed.admin_secret))
    ok = client.post(
        "/login/verify", data={"code": code, "next": "/admin/users", "csrf_token": csrf}
    )
    assert ok.url.path == "/admin/users"


def test_a_totp_code_cannot_be_replayed(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    other = TestClient(client.app, base_url="http://localhost:8000")
    response = other.post(
        "/login", data={"email": installed.admin_email, "password": installed.admin_password}
    )
    code = _fresh_code(pyotp.TOTP(installed.admin_secret))  # the code sign_in just used
    replay = other.post(
        "/login/verify", data={"code": code, "next": "/", "csrf_token": _form_csrf(response.text)}
    )
    assert replay.status_code == 422


def test_a_recovery_code_works_exactly_once(installed: Installed, client: TestClient) -> None:
    response = client.post(
        "/login", data={"email": installed.admin_email, "password": installed.admin_password}
    )
    csrf = _form_csrf(response.text)
    code = installed.recovery_codes[0]
    ok = client.post(
        "/login/verify",
        data={"code": code.lower(), "recovery": "true", "next": "/", "csrf_token": csrf},
    )
    assert ok.url.path == "/"

    again = TestClient(client.app, base_url="http://localhost:8000")
    response = again.post(
        "/login", data={"email": installed.admin_email, "password": installed.admin_password}
    )
    reused = again.post(
        "/login/verify",
        data={
            "code": code,
            "recovery": "true",
            "next": "/",
            "csrf_token": _form_csrf(response.text),
        },
    )
    assert reused.status_code == 422


def test_sessions_expire_after_30_idle_minutes(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    assert client.get("/", follow_redirects=False).status_code == 200
    with patch("app.core.clock.now", return_value=clock.now() + timedelta(minutes=31)):
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_sessions_end_after_12_hours_however_active(
    installed: Installed, client: TestClient
) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    start = clock.now()
    for minutes in range(20, 12 * 60, 20):  # active every 20 minutes
        with patch("app.core.clock.now", return_value=start + timedelta(minutes=minutes)):
            assert client.get("/", follow_redirects=False).status_code == 200
    with patch("app.core.clock.now", return_value=start + timedelta(hours=12, minutes=1)):
        assert client.get("/", follow_redirects=False).status_code == 303


def test_signing_out_ends_the_session(installed: Installed, client: TestClient) -> None:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    csrf = _form_csrf(client.get("/me/security").text)
    client.post("/logout", data={"csrf_token": csrf})
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")


def test_session_cookie_flags(installed: Installed, client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"email": installed.admin_email, "password": installed.admin_password},
        follow_redirects=False,
    )
    cookie = response.headers["set-cookie"]
    assert "kx_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
