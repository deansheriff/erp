"""Downloads (`GET /files/{id}`) and Administration → Files."""

import uuid
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core.db import get_db
from app.core.errors import Conflict, ValidationFailed
from app.files import service as files
from app.files.purposes import purposes
from app.identity.schemas import Actor
from app.web.access import require_actor
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]

SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@router.get("/files/{file_id}")
@requires_login
def download(db: Db, actor: CurrentActor, file_id: uuid.UUID) -> Response:
    """View permission is checked on the entity the file belongs to (ADR 0016)."""
    item = files.open_for_download(db, actor, file_id)
    disposition = "inline" if item.inline else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.filename)}",
        "Content-Length": str(item.size),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    if item.mime == "image/svg+xml":
        headers["Content-Security-Policy"] = SVG_POLICY
    return StreamingResponse(item.chunks, media_type=item.mime, headers=headers)


# ------------------------------------------------------------------ administration → files


def _admin_context(
    db: Session,
    actor: Actor,
    *,
    q: str = "",
    purpose: str = "",
    status: str = "",
    held: bool = False,
    page: int = 1,
    selected: str = "",
    **extra: Any,
) -> dict[str, Any]:
    result = files.list_files(db, actor, q=q, purpose=purpose, status=status, held=held, page=page)
    sel = _uuid(selected)
    params = {
        k: v
        for k, v in {
            "q": q,
            "purpose": purpose,
            "status": status,
            "held": "1" if held else "",
        }.items()
        if v
    }
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Files",
            crumbs=["Administration", "Files"],
        ),
        "result": result,
        "usage": files.usage(db),
        "selected": files.file_detail(db, actor, sel) if sel else None,
        "filters": {"q": q, "purpose": purpose, "status": status, "held": held},
        "filtered": bool(params),
        "query": urlencode(params),
        "page_query": urlencode({**params, "page": result.page}),
        "purposes": sorted((p.key, p.label) for p in purposes().values()),
        "can_set_quota": bool(engine.check(db, actor.user_id, "system.storage.manage")),
        "errors": {},
        "page_title": "Files",
        **extra,
    }


@router.get("/admin/files", response_class=HTMLResponse)
@requires("files.file.manage")
@screen("admin.files")
def admin_files(
    request: Request,
    db: Db,
    actor: CurrentActor,
    q: str = "",
    purpose: str = "",
    status: str = "",
    held: int = 0,
    page: int = 1,
    selected: str = "",
    notice: str = "",
) -> HTMLResponse:
    messages = {
        "hold": "Legal hold updated.",
        "deleted": "File deleted. It's removed for good once its retention period ends.",
        "quota": "Storage quota saved.",
    }
    context = _admin_context(
        db,
        actor,
        q=q,
        purpose=purpose,
        status=status,
        held=bool(held),
        page=page,
        selected=selected,
        notice=messages.get(notice),
    )
    return templates.TemplateResponse(request, "files/admin.html", context)


def _back(file_id: uuid.UUID, notice: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/files?selected={file_id}&notice={notice}", status_code=303)


@router.post("/admin/files/{file_id}/hold", response_class=HTMLResponse)
@requires("files.file.manage")
@screen("admin.files")
def legal_hold(
    request: Request,
    db: Db,
    actor: CurrentActor,
    file_id: uuid.UUID,
    on: FormStr = "0",
    reason: FormStr = "",
) -> Response:
    try:
        files.set_legal_hold(db, actor, file_id, on=on == "1", reason=reason)
    except ValidationFailed as exc:
        context = _admin_context(
            db,
            actor,
            selected=str(file_id),
            problem=" ".join(exc.field_errors.values()) or exc.message,
        )
        return templates.TemplateResponse(request, "files/admin.html", context, status_code=422)
    return _back(file_id, "hold")


@router.post("/admin/files/{file_id}/delete", response_class=HTMLResponse)
@requires("files.file.manage")
@screen("admin.files")
def delete_file(request: Request, db: Db, actor: CurrentActor, file_id: uuid.UUID) -> Response:
    try:
        files.delete(db, actor, file_id)
    except Conflict as exc:
        context = _admin_context(db, actor, selected=str(file_id), problem=exc.message)
        return templates.TemplateResponse(request, "files/admin.html", context, status_code=409)
    return _back(file_id, "deleted")


@router.post("/admin/files/quota", response_class=HTMLResponse)
@requires("system.storage.manage")
@screen("admin.files")
def set_quota(request: Request, db: Db, actor: CurrentActor, quota_gb: FormStr = "") -> Response:
    value = quota_gb.strip()
    try:
        if value and not value.replace(".", "", 1).isdigit():
            raise ValidationFailed(field_errors={"quota": "Enter a number of gigabytes."})
        files.set_quota(db, actor, round(float(value) * 1024**3) if value else None)
    except ValidationFailed as exc:
        context = _admin_context(db, actor, errors=exc.field_errors)
        return templates.TemplateResponse(request, "files/admin.html", context, status_code=422)
    return RedirectResponse("/admin/files?notice=quota", status_code=303)
