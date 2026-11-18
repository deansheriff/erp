"""Route declarations. Every route says who may call it; a CI test fails on any route
without a declaration, so nobody can add a route without deciding.

- `@public`             anyone, signed in or not (health checks, sign-in pages)
- `@pending_login`      a signed-in session that hasn't finished two-step verification yet
- `@requires_login`     any fully signed-in person, acting on their own records only
- `@requires("perm")`   a fully signed-in person holding `perm` (policy engine, ADR 0013)

The access guard (app/web/access.py) enforces these for every request.
"""

from collections.abc import Callable
from typing import Any, Literal

PUBLIC_ATTR = "__kx_public__"
PERMISSION_ATTR = "__kx_permission__"
LOGIN_ATTR = "__kx_login__"
SCREEN_ATTR = "__kx_screen__"

LoginLevel = Literal["pending", "full"]


def public[F: Callable[..., Any]](func: F) -> F:
    setattr(func, PUBLIC_ATTR, True)
    return func


def pending_login[F: Callable[..., Any]](func: F) -> F:
    setattr(func, LOGIN_ATTR, "pending")
    return func


def requires_login[F: Callable[..., Any]](func: F) -> F:
    setattr(func, LOGIN_ATTR, "full")
    return func


def requires[F: Callable[..., Any]](permission: str) -> Callable[[F], F]:
    def decorate(func: F) -> F:
        setattr(func, PERMISSION_ATTR, permission)
        setattr(func, LOGIN_ATTR, "full")
        return func

    return decorate


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


def declared_login(func: Callable[..., Any]) -> LoginLevel | None:
    value = getattr(func, LOGIN_ATTR, None)
    return value if value in ("pending", "full") else None


def declared_screen(func: Callable[..., Any]) -> str | None:
    value = getattr(func, SCREEN_ATTR, None)
    return value if isinstance(value, str) else None
