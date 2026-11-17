"""The FastAPI application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.db import get_engine, verify_database_role
from app.core.logging import configure_logging
from app.web import routes_dev, routes_health
from app.web.errors import register_error_handlers
from app.web.middleware import RequestContextMiddleware
from app.web.templating import STATIC_DIR


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.web_threads
    await anyio.to_thread.run_sync(verify_database_role, get_engine())
    yield
    get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Kxmeleon ERP",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.dev_routes_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.dev_routes_enabled else None,
    )
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(routes_health.router)
    if settings.dev_routes_enabled:
        app.include_router(routes_dev.router)
    return app


app = create_app()
