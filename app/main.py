"""The FastAPI application."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app.audit import routes_web as audit_web
from app.authz import routes_web as authz_web
from app.authz import service as authz
from app.core.config import Settings, get_settings
from app.core.db import get_engine, session_scope, verify_database_role
from app.core.logging import configure_logging
from app.files import routes_web as files_web
from app.files import scanner
from app.identity import routes_api as identity_api
from app.identity import routes_web as identity_web
from app.identity import service as identity
from app.modules.attendance import routes_web as attendance_web
from app.modules.expenses import routes_web as expenses_web
from app.modules.finance import routes_banking as finance_banking
from app.modules.finance import routes_payables as finance_payables
from app.modules.finance import routes_sales as finance_sales
from app.modules.finance import routes_tax as finance_tax
from app.modules.finance import routes_web as finance_web
from app.modules.hr import routes_people as hr_people_web
from app.modules.hr import routes_process as hr_process_web
from app.modules.hr import routes_self as hr_self_web
from app.modules.hr import routes_web as hr_web
from app.modules.leave import routes_web as leave_web
from app.modules.payroll import routes_outputs as payroll_outputs_web
from app.modules.payroll import routes_self as payroll_self_web
from app.modules.payroll import routes_web as payroll_web
from app.org import routes_web as org_web
from app.web import routes_dev, routes_health, routes_home
from app.web.access import access_guard, csrf_guard
from app.web.errors import register_error_handlers
from app.web.middleware import RequestContextMiddleware
from app.web.templating import STATIC_DIR
from app.wiring import configure_runtime


def _announce_setup_code() -> None:
    """Until setup is complete, print a fresh one-time setup code to the log (ADR 0002)."""
    with session_scope() as db:
        if identity.is_installed(db):
            return
        code = identity.issue_setup_token(db)
    logging.getLogger("app.setup").warning(
        "kxmeleon_not_set_up",
        extra={"setup_code": code, "open": "/setup", "valid_minutes": identity.SETUP_TOKEN_MINUTES},
    )


def _verify_permission_catalogue() -> None:
    """Refuse to start if a policies.py declares permissions no migration has added
    (or the reverse): grants would silently refer to nothing (ADR 0013)."""
    with session_scope() as db:
        problems = authz.catalogue_problems(db)
    if problems:
        raise RuntimeError("Permission catalogue out of sync: " + "; ".join(problems))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.web_threads
    await anyio.to_thread.run_sync(verify_database_role, get_engine())
    await anyio.to_thread.run_sync(_verify_permission_catalogue)
    scanner.check_configuration()
    await anyio.to_thread.run_sync(_announce_setup_code)
    yield
    get_engine().dispose()


def routers(settings: Settings) -> list[APIRouter]:
    """Every router the app serves. CI walks these to check each route's declarations."""
    included = [
        routes_health.router,
        routes_home.router,
        # Before identity: its POST /admin/users/{id}/{action} would otherwise catch
        # /admin/users/{id}/access.
        authz_web.router,
        identity_web.router,
        identity_api.router,
        audit_web.router,
        org_web.router,
        # Before hr_web: /hr/people/new must not be read as a person ID.
        hr_people_web.router,
        hr_web.router,
        hr_process_web.router,
        hr_self_web.router,
        leave_web.router,
        attendance_web.router,
        expenses_web.router,
        payroll_web.router,
        payroll_outputs_web.router,
        finance_web.router,
        finance_sales.router,
        finance_tax.router,
        finance_payables.router,
        finance_banking.router,
        payroll_self_web.router,
        files_web.router,
    ]
    if settings.dev_routes_enabled:
        included.append(routes_dev.router)
    return included


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_runtime()

    app = FastAPI(
        title="Kxmeleon ERP",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.dev_routes_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.dev_routes_enabled else None,
        # Every route passes the access guard, then the CSRF check (ADR 0009).
        dependencies=[Depends(access_guard), Depends(csrf_guard)],
    )
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    for router in routers(settings):
        app.include_router(router)
    return app


app = create_app()
