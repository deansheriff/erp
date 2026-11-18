"""The access guard: one place that decides whether a request may reach its route.

Runs as app-wide dependencies, in this order:
1. `access_guard` (sync, uses the request's database session):
   - sends everything to /setup until the installation is complete;
   - identifies the actor from the session cookie or, for /api/*, a bearer API key;
   - enforces the route's declaration (app/authz/declarations.py).
2. `csrf_guard` (async, no database): on unsafe methods, checks Origin and the session's
   CSRF token (ADR 0009).
"""

from urllib.parse import quote, urlsplit

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.authz.declarations import declared_login, declared_permission, is_public
from app.core.config import get_settings
from app.core.context import valid_ip
from app.core.db import get_db
from app.core.errors import PermissionDenied, RedirectRequired
from app.core.secrets import tokens_equal
from app.identity import service as identity
from app.identity.schemas import Actor

SESSION_COOKIE = "kx_session"
SETUP_COOKIE = "kx_setup"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
OPEN_DURING_SETUP = ("/setup", "/healthz", "/readyz", "/static/", "/_dev/")


def current_actor(request: Request) -> Actor | None:
    actor = getattr(request.state, "actor", None)
    return actor if isinstance(actor, Actor) else None


def require_actor(request: Request) -> Actor:
    """For routes behind @requires_login / @requires: the guard has already checked."""
    actor = current_actor(request)
    if actor is None:
        raise RedirectRequired("/login")
    return actor


def client_ip(request: Request) -> str | None:
    return valid_ip(request.client.host) if request.client else None


def safe_next(target: str | None) -> str:
    """Only same-site relative paths may be used as a post-login destination."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def access_guard(request: Request, db: Session = Depends(get_db)) -> None:  # noqa: B008
    path = request.url.path
    if not identity.is_installed(db):
        if not path.startswith(OPEN_DURING_SETUP):
            raise RedirectRequired("/setup", code="setup_required")
    elif path.startswith("/setup"):
        raise RedirectRequired("/", code="already_installed")

    if path.startswith("/api/") and _bearer(request):
        actor = identity.resolve_api_key(db, _bearer(request))
    else:
        actor = identity.resolve_session(db, request.cookies.get(SESSION_COOKIE))
    request.state.actor = actor

    endpoint = request.scope.get("endpoint")
    if endpoint is None or is_public(endpoint):
        return
    level = declared_login(endpoint)
    if actor is None:
        target = path + (f"?{request.url.query}" if request.url.query else "")
        raise RedirectRequired(f"/login?next={quote(target, safe='/')}")
    if level == "pending":
        return
    if not actor.mfa_satisfied and (actor.mfa_enabled or actor.mfa_required):
        raise RedirectRequired("/login/verify" if actor.mfa_enabled else "/login/two-step")
    permission = declared_permission(endpoint)
    if permission is not None:
        identity.authorize(actor, permission)
    elif level != "full":
        raise PermissionDenied()  # undeclared routes are denied by default


async def csrf_guard(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    actor = current_actor(request)
    if actor is not None and actor.via == "api_key":
        return  # bearer keys carry no ambient cookie, so CSRF doesn't apply
    expected_origin = urlsplit(get_settings().base_url)
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (expected_origin.scheme, expected_origin.netloc):
            raise PermissionDenied("This request came from another site, so it was blocked.")
    if actor is None or actor.csrf_token is None:
        return  # anonymous forms (sign-in, reset) rely on the Origin check and SameSite
    sent = request.headers.get("x-csrf-token")
    if sent is None and "form" in request.headers.get("content-type", ""):
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not sent or not tokens_equal(sent, actor.csrf_token):
        raise PermissionDenied("Your session expired on this page. Reload and try again.")
