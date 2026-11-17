# 0004. Synchronous routes and database sessions

- Status: Accepted
- Date: 2026-11-17

## Context

The brief forbids blocking database calls inside `async def` routes, and asks us to pick
either sync routes or an async session consistently. The same service functions are also
called from Celery workers, which are synchronous.

## Decision

- HTTP route handlers are plain `def`. FastAPI runs them in its thread pool. The thread
  pool size is set explicitly (`KX_WEB_THREADS`, default 40) at start-up.
- SQLAlchemy uses a **synchronous** engine with the **psycopg 3** driver. One session per
  request, opened by a dependency, committed or rolled back at the end.
- `async def` is allowed **only** for WebSocket endpoints (Phase 12) and ASGI middleware.
  Those never touch the database session directly; they call service functions through
  `anyio.to_thread.run_sync`.
- CI enforces this with an AST check (`tests/ci/test_no_async_routes.py`): any `async def`
  route handler outside the allow-list fails.

## Consequences

Services are ordinary sync functions, usable from routes, workers and tests alike. The
cost is one thread per in-flight request, which is fine for ERP traffic. WebSockets use
Redis pub/sub and only occasionally touch the database.
