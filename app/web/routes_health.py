"""Liveness and readiness probes for Docker and load balancers."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.authz.declarations import public
from app.core.db import check_database, get_engine

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
@public
def healthz() -> dict[str, str]:
    """The process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
@public
def readyz() -> JSONResponse:
    """The app can serve requests: the database answers."""
    try:
        check_database(get_engine())
    except Exception:  # noqa: BLE001  (any failure means not ready; details go to the log)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
