"""Route rules: every route declares who may call it, is synchronous, and every HTML
route has a Stitch screen (brief sections 4.2 and 6.2, ADR 0004)."""

import inspect
from pathlib import Path
from typing import Any

import yaml
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from app.authz.declarations import declared_permission, declared_screen, is_public
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _routes() -> list[APIRoute]:
    return [r for r in create_app().routes if isinstance(r, APIRoute)]


def _screens() -> dict[str, dict[str, Any]]:
    registry = yaml.safe_load((ROOT / "design/stitch/screens.yaml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in registry["screens"]}


def test_every_route_declares_a_permission_or_is_public() -> None:
    undeclared = [
        f"{sorted(r.methods)} {r.path}"
        for r in _routes()
        if not is_public(r.endpoint) and declared_permission(r.endpoint) is None
    ]
    assert undeclared == [], "Add @requires('<permission>') or @public to: " + ", ".join(undeclared)


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
        if not exports:
            missing.append(f"{screen_id}: no Stitch export listed")
        missing += [f"{screen_id}: {e}" for e in exports if not (ROOT / e).is_file()]
        if not s.get("stitch_screens"):
            missing.append(f"{screen_id}: no Stitch screen IDs")
    assert missing == []
