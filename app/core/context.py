"""Per-request context shared by logging and audit."""

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: uuid.UUID
    ip: str | None = None
    user_agent: str | None = None


_current: ContextVar[RequestContext | None] = ContextVar("kx_request_context", default=None)


def set_request_context(ctx: RequestContext | None) -> Token[RequestContext | None]:
    return _current.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _current.reset(token)


def current_request() -> RequestContext | None:
    return _current.get()
