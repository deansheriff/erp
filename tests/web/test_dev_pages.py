"""The shell, components and system pages render from the real templates."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=True)


def test_shell_preview_renders_sidebar_topbar_and_mobile_nav(client: TestClient) -> None:
    html = client.get("/_dev/shell").text
    assert 'class="module-expenses"' in html
    assert "Adeyemi Holdings Ltd" in html
    assert 'aria-current="page"' in html  # active module and section
    assert "Search people, claims, invoices" in html
    assert "New request" in html  # mobile bottom navigation
    assert "₦128,750.50" in html
    assert "(₦3,500.00)" in html  # accounting format for negatives


def test_components_gallery_renders_every_component(client: TestClient) -> None:
    html = client.get("/_dev/components").text
    for text in (
        "Submit claim",
        "Scanning for viruses",
        "Blocked: virus found",
        "Rejected: file type not allowed",
        "No claims yet",
        "Post payroll for October 2026?",
        "Manager approval",
    ):
        assert text in html


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("403", "You don't have access to this page"),
        ("404", "We couldn't find that page"),
        ("500", "Something went wrong on our side"),
    ],
)
def test_system_pages_render(client: TestClient, kind: str, text: str) -> None:
    assert text in client.get(f"/_dev/errors/{kind}").text


def test_pages_load_no_third_party_assets(client: TestClient) -> None:
    html = client.get("/_dev/shell").text
    for host in ("cdn.tailwindcss.com", "fonts.googleapis.com", "unpkg.com", "jsdelivr"):
        assert host not in html
    assert "<script>" not in html  # no inline scripts (CSP script-src 'self')
    assert " style=" not in html  # no inline styles (CSP style-src 'self')


def test_dev_routes_are_not_mounted_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("KX_ENV", "production")
    get_settings.cache_clear()
    try:
        prod = TestClient(create_app())
        assert prod.get("/_dev/shell").status_code == 404
    finally:
        monkeypatch.setenv("KX_ENV", "test")
        get_settings.cache_clear()
