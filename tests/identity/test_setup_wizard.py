"""First-run installation (ADR 0002, docs/IDENTITY.md section 3)."""

import html
import re

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.core.db import session_scope
from app.identity import service as identity

pytestmark = pytest.mark.db


def _code() -> str:
    with session_scope() as db:
        return identity.issue_setup_token(db)


def test_everything_redirects_to_setup_until_installed(clean_db: None, client: TestClient) -> None:
    for path in ("/", "/login", "/me/security", "/admin/users"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
    assert client.get("/healthz").status_code == 200


def test_a_wrong_setup_code_is_refused(clean_db: None, client: TestClient) -> None:
    _code()
    response = client.post(
        "/setup/organisation", data={"setup_code": "AAAA-BBBB-CCCC", "legal_name": "Acme Ltd"}
    )
    assert response.status_code == 422
    assert "isn't valid or has expired" in html.unescape(response.text)


def test_full_setup_creates_org_super_admin_and_closes_setup(
    clean_db: None, client: TestClient
) -> None:
    code = _code()
    response = client.post(
        "/setup/organisation",
        data={
            "setup_code": code,
            "legal_name": "Adeyemi Holdings Limited",
            "timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 200
    page = client.get("/setup").text
    secret = re.search(r"<code[^>]*>([A-Z2-7 ]+)</code>", page).group(1).replace(" ", "")  # type: ignore[union-attr]

    response = client.post(
        "/setup/admin",
        data={
            "full_name": "Fatima Bello",
            "email": "fatima.bello@adeyemiholdings.ng",
            "password": "Correct-Horse-Battery-71",
            "password_confirm": "Correct-Horse-Battery-71",
            "mfa_code": pyotp.TOTP(secret).now(),
        },
    )
    assert response.status_code == 200
    assert "Two-step verification is on" in response.text
    assert len(re.findall(r"[A-Z0-9]{4}-[A-Z0-9]{4}", response.text)) >= 10

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/setup").text).group(1)  # type: ignore[union-attr]
    response = client.post("/setup/mail", data={"action": "skip", "csrf_token": csrf})
    assert response.url.path == "/"
    assert "Welcome, Fatima" in response.text

    # Setup is closed for good.
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/"
    with session_scope() as db:
        assert identity.is_installed(db)


def test_setup_rejects_a_weak_password_and_a_bad_mfa_code(
    clean_db: None, client: TestClient
) -> None:
    code = _code()
    client.post("/setup/organisation", data={"setup_code": code, "legal_name": "Adeyemi Holdings"})
    client.get("/setup")
    response = client.post(
        "/setup/admin",
        data={
            "full_name": "Fatima Bello",
            "email": "fatima.bello@adeyemiholdings.ng",
            "password": "password1234",
            "password_confirm": "password1234",
            "mfa_code": "000000",
        },
    )
    assert response.status_code == 422
    assert "too common" in response.text
    assert "That code didn't work" in html.unescape(response.text)


def test_setup_tokens_are_single_purpose_and_expire(clean_db: None) -> None:
    from datetime import timedelta
    from unittest.mock import patch

    from app.core import clock

    code = _code()
    with session_scope() as db:
        assert identity.check_setup_token(db, code)
        assert identity.check_setup_token(db, code.lower())  # typed in lower case
    later = clock.now() + timedelta(minutes=61)
    with patch("app.core.clock.now", return_value=later), session_scope() as db:
        assert not identity.check_setup_token(db, code)
