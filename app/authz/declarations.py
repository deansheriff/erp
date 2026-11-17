"""Route declarations. Every route is either `@public` or `@requires(...)` (Phase 3).

A CI test walks the route table and fails on any route with neither, so a new route
can't be added without someone deciding who may call it.
"""

from collections.abc import Callable
from typing import Any

PUBLIC_ATTR = "__kx_public__"
PERMISSION_ATTR = "__kx_permission__"
SCREEN_ATTR = "__kx_screen__"


def public[F: Callable[..., Any]](func: F) -> F:
    """Mark a route as callable without authentication (health checks, static pages)."""
    setattr(func, PUBLIC_ATTR, True)
    return func


def screen[F: Callable[..., Any]](screen_id: str) -> Callable[[F], F]:
    """Link an HTML route to its entry in design/stitch/screens.yaml."""

    def decorate(func: F) -> F:
        setattr(func, SCREEN_ATTR, screen_id)
        return func

    return decorate


def is_public(func: Callable[..., Any]) -> bool:
    return bool(getattr(func, PUBLIC_ATTR, False))


def declared_permission(func: Callable[..., Any]) -> str | None:
    value = getattr(func, PERMISSION_ATTR, None)
    return value if isinstance(value, str) else None


def declared_screen(func: Callable[..., Any]) -> str | None:
    value = getattr(func, SCREEN_ATTR, None)
    return value if isinstance(value, str) else None
