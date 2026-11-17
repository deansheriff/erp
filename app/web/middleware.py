"""ASGI middleware: request ID, request context and security headers."""

import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import RequestContext, reset_request_context, set_request_context
from app.web.errors import unhandled_error_handler

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(self), geolocation=(self), microphone=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _incoming_request_id(scope: Scope) -> uuid.UUID:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            try:
                return uuid.UUID(value.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                break
    return uuid.uuid4()


class RequestContextMiddleware:
    """Assigns a request ID, exposes it to logs and audit, and adds security headers."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        client = scope.get("client")
        user_agent = next(
            (v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"user-agent"),
            None,
        )
        ctx = RequestContext(
            request_id=request_id,
            ip=client[0] if client else None,
            user_agent=user_agent,
        )
        scope.setdefault("state", {})["request_id"] = request_id

        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = str(request_id)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if self.hsts:
                    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        token = set_request_context(ctx)
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            # Unexpected errors are answered here rather than by Starlette's outermost
            # handler, so the error page still carries the request ID and security headers.
            if response_started:
                raise
            response = await unhandled_error_handler(Request(scope, receive), exc)
            await response(scope, receive, send_with_headers)
        finally:
            reset_request_context(token)
