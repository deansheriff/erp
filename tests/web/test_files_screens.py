"""Files through the browser's eyes: logo upload (multipart, streamed), the CSRF header
rule for uploads, download headers, and Administration → Files."""

import html
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.files import service as files
from app.files.models import StoredFile
from tests.conftest import Installed, csrf_token, sign_in
from tests.files.conftest import SAFE_SVG, png
from tests.identity.test_accounts import _accept, _invite

pytestmark = pytest.mark.db


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _upload_logo(client: TestClient, data: bytes, name: str = "logo.png", *, header: bool = True):  # type: ignore[no-untyped-def]
    headers = {"X-CSRF-Token": csrf_token(client)} if header else {}
    return client.post(
        "/admin/organisation/logo", files={"file": (name, data, "image/png")}, headers=headers
    )


def _latest() -> StoredFile:
    with session_scope() as db:
        row = db.scalars(select(StoredFile).order_by(StoredFile.created_at.desc())).first()
        assert row is not None
        db.expunge(row)
        return row


def test_logo_upload_scan_and_display(admin: TestClient) -> None:
    response = _upload_logo(admin, png())
    assert response.status_code == 200, response.text[:300]
    assert "virus check" in html.unescape(response.text)
    assert "Scanning for viruses" in response.text
    files.scan_pending()
    page = admin.get("/admin/organisation").text
    row = _latest()
    assert f'src="/files/{row.id}"' in page  # shown on the page and in the sidebar
    download = admin.get(f"/files/{row.id}")
    assert download.status_code == 200 and download.content == png()
    assert download.headers["content-type"] == "image/png"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"].startswith("inline;")


def test_svg_logos_get_a_locked_down_policy(admin: TestClient) -> None:
    assert _upload_logo(admin, SAFE_SVG, "logo.svg").status_code == 200
    files.scan_pending()
    response = admin.get(f"/files/{_latest().id}")
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in response.headers["content-security-policy"]


def test_uploads_must_carry_the_csrf_header(admin: TestClient) -> None:
    response = _upload_logo(admin, png(), header=False)
    assert response.status_code == 403


def test_rejections_show_on_the_page(admin: TestClient) -> None:
    too_big = _upload_logo(admin, png() + b"\0" * (3 * 1024 * 1024))
    assert too_big.status_code == 422 and "too large" in too_big.text
    wrong = _upload_logo(admin, b"%PDF-1.7 pretending", "logo.png")
    assert wrong.status_code == 422 and "isn&#39;t accepted" in wrong.text


def test_people_without_branding_rights_cant_upload_but_can_see_the_logo(
    installed: Installed, client: TestClient
) -> None:
    token = _invite(client, installed)  # signs the admin in
    employee = _accept(token, client.app)
    _upload_logo(client, png())
    files.scan_pending()
    logo = _latest()
    page = employee.get("/")
    assert f'src="/files/{logo.id}"' in page.text
    assert employee.get(f"/files/{logo.id}").status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', employee.get("/me/security").text)
    assert match
    response = employee.post(
        "/admin/organisation/logo",
        files={"file": ("x.png", png(), "image/png")},
        headers={"X-CSRF-Token": match.group(1)},
    )
    assert response.status_code == 403
    assert employee.get("/admin/files").status_code == 403


def test_files_admin_lists_details_and_holds(admin: TestClient) -> None:
    _upload_logo(admin, png())
    row = _latest()
    page = admin.get("/admin/files")
    assert page.status_code == 200 and "logo.png" in page.text and "Scanning" in page.text
    detail = admin.get(f"/admin/files?selected={row.id}").text
    assert row.sha256 in detail
    response = admin.post(
        f"/admin/files/{row.id}/hold",
        data={"on": "1", "reason": "Pending tax audit", "csrf_token": csrf_token(admin)},
    )
    assert response.status_code == 200 and "Legal hold updated" in response.text
    response = admin.post(f"/admin/files/{row.id}/delete", data={"csrf_token": csrf_token(admin)})
    assert response.status_code == 409 and "legal hold" in response.text


def test_the_quota_can_be_changed_by_super_admins(admin: TestClient) -> None:
    response = admin.post(
        "/admin/files/quota", data={"quota_gb": "10", "csrf_token": csrf_token(admin)}
    )
    assert response.status_code == 200 and "10 GB" in response.text
