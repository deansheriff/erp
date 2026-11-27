"""Chat web routes (ADR 0034): the chat page with its list and an open conversation,
the fragments the page fetches when a notice arrives, sending and changing messages,
new conversations, details and settings. Every call goes through a service that checks
membership; a non-member gets "not found"."""

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.chat import common, conversations, messages, notices
from app.modules.chat.files import CHAT_ATTACHMENT
from app.modules.chat.models import BODY_LIMIT, REACTIONS
from app.web.access import require_actor
from app.web.forms import all_fields
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
Fields = Annotated[dict[str, str], Depends(all_fields)]
Upload = Annotated[ReceivedUpload, Depends(upload_body(CHAT_ATTACHMENT.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
USE = "chat.conversation.use"
NOTICES = {
    "created": "Group created. Everyone you added can see it now.",
    "renamed": "Renamed.",
    "added": "Added.",
    "removed": "Removed from the group.",
    "admin": "They're an admin now.",
    "left": "You've left the group.",
    "saved": "Saved.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _shell(db: Session, actor: Actor, section: str, *crumbs: str) -> Any:
    return build_shell(
        db, actor, active_module="chat", active_section=section, crumbs=["Chat", *crumbs]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _common() -> dict[str, Any]:
    return {"kind_labels": common.KIND_LABELS, "reactions": REACTIONS, "body_limit": BODY_LIMIT}


# ------------------------------------------------------------------ the chat page


def _page_context(
    db: Session, actor: Actor, conversation_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "shell": _shell(db, actor, "Chat"),
        "current": None,
        "page_title": "Chat",
        "values": {},
        "errors": {},
        **_common(),
    }
    if conversation_id is not None:
        found = common.access(db, actor, conversation_id)
        unread_after = found.member.last_read_message_id
        context.update(
            current=found.conversation,
            title=common.title(db, found.conversation, actor.user_id),
            members=len(common.member_ids(db, conversation_id)),
            page=messages.page(db, actor, conversation_id),
            unread_after=unread_after,
            pins=messages.pinned(db, actor, conversation_id),
            page_title=common.title(db, found.conversation, actor.user_id),
        )
        messages.mark_read(db, actor, conversation_id)
        context["receipt"] = messages.receipt(db, actor, conversation_id)
    # After marking read, so the open conversation's unread count is already gone.
    context["rows"] = conversations.list_for(db, actor)
    context.update(extra)
    return context


@router.get("/chat", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def chat_home(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "chat/home.html", _page_context(db, actor, None))


@router.get("/chat/{conversation_id:uuid}", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def conversation_page(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _page_context(db, actor, conversation_id, notice=NOTICES.get(notice))
    return _render(request, "chat/home.html", context)


# ------------------------------------------------------------------ fragments


@router.get("/chat/list", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def list_fragment(request: Request, db: Db, actor: CurrentActor, current: str = "") -> HTMLResponse:
    context = {
        "rows": conversations.list_for(db, actor, sync=False),
        "current_id": common.uuid_or_none(current),
        **_common(),
    }
    return _render(request, "chat/_list.html", context)


@router.get("/chat/{conversation_id:uuid}/messages", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def messages_fragment(
    request: Request,
    db: Db,
    actor: CurrentActor,
    conversation_id: uuid.UUID,
    after: str = "",
    before: str = "",
) -> HTMLResponse:
    """Newer messages (marking them read) or an older page."""
    newer = common.uuid_or_none(after)
    page = messages.page(
        db, actor, conversation_id, after=newer, before=common.uuid_or_none(before)
    )
    if newer is not None or not before:
        messages.mark_read(db, actor, conversation_id)
    context = {
        "page": page,
        "current": page.access.conversation,
        "older": bool(before),
        **_common(),
    }
    return _render(request, "chat/_messages.html", context)


@router.get("/chat/{conversation_id:uuid}/messages/{message_id:uuid}", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def message_fragment(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> HTMLResponse:
    view = messages.one(db, actor, conversation_id, message_id)
    context = {"m": view, "conversation_id": conversation_id, **_common()}
    return _render(request, "chat/_message.html", context)


@router.get("/chat/{conversation_id:uuid}/receipt", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def receipt_fragment(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID
) -> HTMLResponse:
    context = {"receipt": messages.receipt(db, actor, conversation_id)}
    return _render(request, "chat/_receipt.html", context)


@router.get("/chat/{conversation_id:uuid}/pins", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def pins_fragment(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID
) -> HTMLResponse:
    context = {
        "pins": messages.pinned(db, actor, conversation_id),
        "conversation_id": conversation_id,
    }
    return _render(request, "chat/_pins.html", context)


# ------------------------------------------------------------------ sending and changing


def _composer_error(
    request: Request, db: Session, actor: Actor, cid: uuid.UUID, error: str
) -> Response:
    if _htmx(request):
        # Shown under the composer without reloading the conversation.
        return HTMLResponse(
            templates.get_template("chat/_composer_error.html").render(error=error),
            headers={"HX-Retarget": "#chat-composer-error", "HX-Reswap": "innerHTML"},
        )
    context = _page_context(db, actor, cid, errors={"body": error})
    return _render(request, "chat/home.html", context, 422)


@router.post("/chat/{conversation_id:uuid}/messages", response_class=HTMLResponse)
@requires(USE)
@screen("chat.home")
def send_message(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID, upload: Upload
) -> Response:
    try:
        if upload.errors:
            raise ValidationFailed(field_errors={"body": next(iter(upload.errors.values()))})
        with db.begin_nested():
            messages.send(
                db,
                actor,
                conversation_id,
                upload.fields.get("body", ""),
                messages.Upload(upload.filename, upload.content_type, upload.data),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        return _composer_error(
            request, db, actor, conversation_id, errors.get("body") or next(iter(errors.values()))
        )
    if upload.data:
        files.request_scan()
    if _htmx(request):
        return Response(status_code=204, headers={"HX-Trigger": "chat-sent"})
    return _see_other(f"/chat/{conversation_id}")


def _message_action(
    db: Session,
    actor: Actor,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    action: str,
    f: dict[str, str],
) -> None:
    runs: dict[str, Callable[[], object]] = {
        "edit": lambda: messages.edit(db, actor, conversation_id, message_id, f.get("body", "")),
        "delete": lambda: messages.delete_message(db, actor, conversation_id, message_id),
        "react": lambda: messages.react(db, actor, conversation_id, message_id, f.get("emoji", "")),
        "pin": lambda: messages.pin(db, actor, conversation_id, message_id),
    }
    if action not in runs:
        raise NotFound()
    runs[action]()


@router.post(
    "/chat/{conversation_id:uuid}/messages/{message_id:uuid}/{action}",
    response_class=HTMLResponse,
)
@requires(USE)
@screen("chat.home")
def message_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    action: str,
    f: Fields,
) -> Response:
    error = None
    try:
        with db.begin_nested():
            _message_action(db, actor, conversation_id, message_id, action, f)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        error = errors.get("body") or next(iter(errors.values()))
    if _htmx(request):
        view = messages.one(db, actor, conversation_id, message_id)
        context = {"m": view, "conversation_id": conversation_id, "error": error, **_common()}
        return _render(request, "chat/_message.html", context)
    if error:
        context = _page_context(db, actor, conversation_id, errors={"form": error})
        return _render(request, "chat/home.html", context, 422)
    return _see_other(f"/chat/{conversation_id}")


# ------------------------------------------------------------------ new conversations


def _new_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    common.require(db, actor, common.USE)
    return {
        "shell": _shell(db, actor, "Chat", "New conversation"),
        "people": conversations.people(db, actor),
        "values": {},
        "errors": {},
        "page_title": "New conversation",
        **extra,
    }


@router.get("/chat/new", response_class=HTMLResponse)
@requires(USE)
@screen("chat.new")
def new_conversation(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "chat/new.html", _new_context(db, actor))


@router.post("/chat/new/direct", response_class=HTMLResponse)
@requires(USE)
@screen("chat.new")
def open_direct(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    try:
        with db.begin_nested():
            cid = conversations.open_direct(db, actor, f.get("person_id", ""))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _new_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "chat/new.html", context, 422)
    return _see_other(f"/chat/{cid}")


@router.post("/chat/new/group", response_class=HTMLResponse)
@requires(USE)
@screen("chat.new")
def create_group(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    chosen = [k.removeprefix("member_") for k in f if k.startswith("member_")]
    try:
        with db.begin_nested():
            cid = conversations.create_group(db, actor, f.get("title", ""), chosen)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _new_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "chat/new.html", context, 422)
    return _see_other(f"/chat/{cid}?notice=created")


# ------------------------------------------------------------------ details


def _details_context(
    db: Session, actor: Actor, conversation_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    details = conversations.details(db, actor, conversation_id)
    in_group = {m.user_id for m in details.members}
    return {
        "shell": _shell(db, actor, "Chat", details.title),
        "d": details,
        "c": details.access.conversation,
        "me": actor.user_id,
        "addable": [p for p in conversations.people(db, actor) if p[0] not in in_group]
        if details.is_admin
        else [],
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{details.title} · details",
        **_common(),
        **extra,
    }


@router.get("/chat/{conversation_id:uuid}/details", response_class=HTMLResponse)
@requires(USE)
@screen("chat.details")
def details_page(
    request: Request, db: Db, actor: CurrentActor, conversation_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _details_context(db, actor, conversation_id, notice=NOTICES.get(notice))
    return _render(request, "chat/details.html", context)


def _details_action(
    db: Session, actor: Actor, conversation_id: uuid.UUID, action: str, f: dict[str, str]
) -> str:
    runs: dict[str, tuple[Any, str]] = {
        "rename": (
            lambda: conversations.set_title(db, actor, conversation_id, f.get("title", "")),
            "renamed",
        ),
        "add": (
            lambda: conversations.add_members(db, actor, conversation_id, [f.get("user_id", "")]),
            "added",
        ),
        "remove": (
            lambda: conversations.remove_member(db, actor, conversation_id, f.get("user_id", "")),
            "removed",
        ),
        "admin": (
            lambda: conversations.make_admin(db, actor, conversation_id, f.get("user_id", "")),
            "admin",
        ),
        "leave": (lambda: conversations.leave(db, actor, conversation_id), "left"),
    }
    if action not in runs:
        raise NotFound()
    run, notice = runs[action]
    run()
    return notice


@router.post("/chat/{conversation_id:uuid}/details/{action}", response_class=HTMLResponse)
@requires(USE)
@screen("chat.details")
def details_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    conversation_id: uuid.UUID,
    action: str,
    f: Fields,
) -> Response:
    try:
        with db.begin_nested():
            notice = _details_action(db, actor, conversation_id, action, f)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _details_context(db, actor, conversation_id, values=f, errors=_errors(exc))
        return _render(request, "chat/details.html", context, 422)
    if notice == "left":
        return _see_other("/chat")
    return _see_other(f"/chat/{conversation_id}/details?notice={notice}")


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    common.require(db, actor, common.SETTINGS)
    row = common.settings(db)
    return {
        "shell": _shell(db, actor, "Settings", "Settings"),
        "values": {
            "edit_minutes": str(row.edit_minutes),
            "email_after_minutes": str(row.email_after_minutes),
            "retention_days": str(row.retention_days or ""),
        },
        "errors": {},
        "notice": None,
        "page_title": "Chat settings",
        **extra,
    }


@router.get("/chat/settings", response_class=HTMLResponse)
@requires("chat.settings.manage")
@screen("chat.settings")
def settings_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _settings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "chat/settings.html", context)


@router.post("/chat/settings", response_class=HTMLResponse)
@requires("chat.settings.manage")
@screen("chat.settings")
def save_settings(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    data = notices.SettingsInput(
        f.get("edit_minutes", ""), f.get("email_after_minutes", ""), f.get("retention_days", "")
    )
    try:
        with db.begin_nested():
            notices.save_settings(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "chat/settings.html", context, 422)
    return _see_other("/chat/settings?notice=saved")
