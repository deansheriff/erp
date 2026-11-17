# 0008. Structured logging and domain errors

- Status: Accepted
- Date: 2026-11-17

## Context

Users must see clear messages while internals never leak (no `str(exception)` in
responses). Operators need logs they can search by request.

## Decision

- **Logging:** standard library `logging` with a small JSON formatter
  (`app/core/logging.py`). No extra dependency. Every record carries `request_id`, and
  `user_id` once identity exists. Values of sensitive field classes are never logged.
- **Request ID:** taken from a valid incoming `X-Request-ID` (UUID format only),
  otherwise generated. Echoed in the response header and shown on error pages, so users
  can quote it to support.
- **Domain errors** live in `app/core/errors.py`: `DomainError` (with a stable `code`
  and a user-safe `message`), `NotFound`, `PermissionDenied`, `ValidationFailed`,
  `Conflict`. Services raise them. One set of exception handlers translates them into
  HTTP responses: JSON for `/api/*`, an HTML page or fragment for web routes (HTMX
  requests get a toast fragment).
- Any other exception becomes a generic 500 ("Something went wrong on our side.
  Reference: <request id>"), and the traceback goes only to the log.
- A CI check rejects `str(exc)`, `repr(exc)` and f-string interpolation of the caught
  exception inside route modules and exception handlers.

## Consequences

One consistent error experience. Messages shown to users are written deliberately, in
the domain layer, rather than leaking from libraries.
