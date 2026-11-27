"""Route rules: every route declares who may call it, is synchronous, and every HTML
route has a Stitch screen (brief sections 4.2 and 6.2, ADR 0004)."""

import inspect
from pathlib import Path
from typing import Any

import yaml
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from app.authz.declarations import (
    declared_login,
    declared_permission,
    declared_screen,
    is_public,
)
from app.core.config import get_settings
from app.main import routers

ROOT = Path(__file__).resolve().parents[2]


def _routes() -> list[APIRoute]:
    routes = [
        r for router in routers(get_settings()) for r in router.routes if isinstance(r, APIRoute)
    ]
    assert len(routes) >= 20, "the route walk found too few routes; has FastAPI's API changed?"
    return routes


def _screens() -> dict[str, dict[str, Any]]:
    registry = yaml.safe_load((ROOT / "design/stitch/screens.yaml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in registry["screens"]}


def test_every_route_declares_a_permission_or_is_public() -> None:
    undeclared = [
        f"{sorted(r.methods)} {r.path}"
        for r in _routes()
        if not is_public(r.endpoint)
        and declared_permission(r.endpoint) is None
        and declared_login(r.endpoint) is None
    ]
    assert undeclared == [], (
        "Add @public, @pending_login, @requires_login or @requires('<permission>') to: "
        + ", ".join(undeclared)
    )


def test_no_route_handler_is_async() -> None:
    async_routes = [r.path for r in _routes() if inspect.iscoroutinefunction(r.endpoint)]
    assert async_routes == [], f"Route handlers must be plain def (ADR 0004): {async_routes}"


def test_every_html_route_is_registered_in_screens_yaml() -> None:
    screens = _screens()
    problems = []
    for r in _routes():
        if r.response_class is not HTMLResponse:
            continue
        screen_id = declared_screen(r.endpoint)
        if screen_id is None:
            problems.append(f"{r.path}: missing @screen(...)")
        elif screen_id not in screens:
            problems.append(f"{r.path}: screen '{screen_id}' not in screens.yaml")
    assert problems == []


def test_every_screen_entry_has_its_template_and_stitch_exports() -> None:
    missing = []
    for screen_id, s in _screens().items():
        for key in ("template", "preview_template"):
            if s.get(key) and not (ROOT / s[key]).is_file():
                missing.append(f"{screen_id}: {s[key]}")
        exports = s.get("exports") or []
        missing += [f"{screen_id}: {e}" for e in exports if not (ROOT / e).is_file()]
        if s.get("stitch_pending"):
            # Requested from Stitch but not delivered yet: built from the design system
            # meanwhile, and the ids and exports are added when it arrives.
            continue
        if not exports:
            missing.append(f"{screen_id}: no Stitch export listed")
        if not s.get("stitch_screens"):
            missing.append(f"{screen_id}: no Stitch screen IDs")
    assert missing == []


def test_the_only_websocket_is_the_chat_socket() -> None:
    """WebSocket routes skip the HTTP access guard and do their own origin and session
    checks (ADR 0034), so each one has to be known and tested. Only /ws/chat exists."""
    from starlette.routing import WebSocketRoute

    sockets = [
        r.path
        for router in routers(get_settings())
        for r in router.routes
        if isinstance(r, WebSocketRoute) or "websocket" in type(r).__name__.lower()
    ]
    assert sockets == ["/ws/chat"]
