"""Turn domain errors and unexpected exceptions into responses (ADR 0008).

Rules: messages come from DomainError.message, which is written for users. Exception
text is never put into a response. Unexpected errors are logged with their traceback
and shown as a generic message with the request ID.
"""

import logging
import uuid
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import Conflict, DomainError, NotFound, PermissionDenied, ValidationFailed
from app.web.templating import templates

log = logging.getLogger("app.errors")

_STATUS: dict[type[DomainError], int] = {
    NotFound: 404,
    PermissionDenied: 403,
    ValidationFailed: 422,
    Conflict: 409,
}

_PAGE_TEMPLATES = {
    403: "errors/no_access.html",
    404: "errors/not_found.html",
}

GENERIC_MESSAGE = "Something went wrong on our side. Please try again in a moment."


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if isinstance(value, uuid.UUID) else ""


def _render(
    request: Request,
    status: int,
    code: str,
    message: str,
    field_errors: dict[str, str] | None = None,
) -> Response:
    request_id = _request_id(request)
    if _wants_json(request):
        body: dict[str, Any] = {"error": {"code": code, "message": message}}
        if field_errors:
            body["error"]["fields"] = field_errors
        body["error"]["request_id"] = request_id
        return JSONResponse(body, status_code=status)

    context = {"status": status, "message": message, "request_id": request_id}
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request,
            "components/toast_error.html",
            context,
            status_code=status,
            headers={"HX-Reswap": "none"},
        )
    template = _PAGE_TEMPLATES.get(status, "errors/server_error.html")
    return templates.TemplateResponse(request, template, context, status_code=status)


async def domain_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, DomainError):
        raise exc
    status = next((s for cls, s in _STATUS.items() if isinstance(exc, cls)), 400)
    field_errors = exc.field_errors if isinstance(exc, ValidationFailed) else None
    return _render(request, status, exc.code, exc.message, field_errors)


async def http_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    status = exc.status_code
    if status == 404:
        return _render(request, 404, NotFound.code, NotFound.default_message)
    if status == 403:
        return _render(request, 403, PermissionDenied.code, PermissionDenied.default_message)
    phrase = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error"
    return _render(request, status, "http_error", phrase)


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        raise exc
    fields = {
        ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")): str(
            err.get("msg", "Invalid value")
        )
        for err in exc.errors()
    }
    return _render(request, 422, ValidationFailed.code, ValidationFailed.default_message, fields)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", exc_info=exc, extra={"path": request.url.path})
    return _render(request, 500, "server_error", GENERIC_MESSAGE)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Anything else is caught by RequestContextMiddleware, which calls
    # unhandled_error_handler so the response keeps its request ID and headers.
