"""Error handling: users see clear messages; internals never leak (ADR 0008)."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authz.declarations import public
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.main import create_app

SECRET_DETAIL = "psycopg.OperationalError: password for kx_app at 10.0.0.5"


@pytest.fixture()
def app() -> FastAPI:
    app = create_app()

    @app.get("/boom")
    @public
    def boom() -> None:
        raise RuntimeError(SECRET_DETAIL)

    @app.get("/api/missing")
    @public
    def missing() -> None:
        raise NotFound()

    @app.get("/api/invalid")
    @public
    def invalid() -> None:
        raise ValidationFailed(field_errors={"amount": "Enter an amount greater than ₦0.00"})

    @app.get("/forbidden")
    @public
    def forbidden() -> None:
        raise PermissionDenied()

    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    # No `with`: the lifespan (database role check) is not needed for these tests.
    return TestClient(app, raise_server_exceptions=False)


def test_unexpected_errors_never_leak_exception_text(client: TestClient) -> None:
    response = client.get("/boom")
    assert response.status_code == 500
    assert SECRET_DETAIL not in response.text
    assert "psycopg" not in response.text
    assert "Something went wrong on our side" in response.text
    request_id = response.headers["X-Request-ID"]
    assert request_id in response.text  # the reference users can quote to support


def test_api_errors_are_json_with_a_stable_code(client: TestClient) -> None:
    response = client.get("/api/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_validation_errors_carry_field_messages(client: TestClient) -> None:
    response = client.get("/api/invalid")
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"amount": "Enter an amount greater than ₦0.00"}


def test_permission_denied_renders_the_no_access_page(client: TestClient) -> None:
    response = client.get("/forbidden")
    assert response.status_code == 403
    assert "You don't have access to this page" in response.text


def test_unknown_pages_render_not_found(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "We couldn't find that page" in response.text


def test_htmx_errors_return_a_toast_without_replacing_the_page(client: TestClient) -> None:
    response = client.get("/forbidden", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert response.headers["HX-Reswap"] == "none"
    assert 'hx-swap-oob="beforeend"' in response.text


def test_security_headers_are_set_on_every_response(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_a_valid_incoming_request_id_is_kept(client: TestClient) -> None:
    rid = str(uuid.uuid4())
    response = client.get("/healthz", headers={"X-Request-ID": rid})
    assert response.headers["X-Request-ID"] == rid


def test_a_malformed_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "<script>"})
    assert uuid.UUID(response.headers["X-Request-ID"])
