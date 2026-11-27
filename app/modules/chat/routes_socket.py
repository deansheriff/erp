"""The chat WebSocket, `/ws/chat` (ADR 0034).

This is the only `async def` handler (ADR 0004), and its database work runs in a worker
thread. It's a plain Starlette route, so the HTTP access guard doesn't run; it does its
own checks:

- **Origin:** the Origin header must be the site's own, or the connection is refused.
- **Session:** the session cookie must resolve to a user with MFA satisfied and
  `chat.conversation.use`.
- **Re-checks:** every `ws_recheck_seconds`, and at once on a "sessions revoked" notice.
  Re-checks don't count as activity, so an open tab doesn't keep an idle session alive.
  When the session has gone, the socket closes with code 4401.

It only forwards small notices from the user's channel. The page fetches content
through ordinary routes, which check membership.
"""

import asyncio
import json
import time
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.authz import engine
from app.core.config import get_settings
from app.core.db import session_scope
from app.identity import service as identity
from app.identity.schemas import Actor
from app.modules.chat import common
from app.realtime import broker as realtime
from app.web.access import SESSION_COOKIE

PATH = "/ws/chat"
UNAUTHORISED = 4401
FORBIDDEN_ORIGIN = 4403


def _allowed(raw: str | None, *, touch: bool) -> Actor | None:
    """The actor if the session is live, MFA is satisfied and they may use chat."""
    if not raw:
        return None
    engine.begin_request_cache()
    with session_scope() as db:
        actor = identity.resolve_session(db, raw, touch=touch)
        if actor is None:
            return None
        if not actor.mfa_satisfied and (actor.mfa_enabled or actor.mfa_required):
            return None
        if not common.can(db, actor.user_id, common.USE):
            return None
        return actor


def _same_origin(origin: str | None) -> bool:
    if not origin:
        return False
    site, sent = urlsplit(get_settings().base_url), urlsplit(origin)
    return (sent.scheme, sent.netloc) == (site.scheme, site.netloc)


async def _until_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


async def _still_allowed(raw: str | None) -> bool:
    return await anyio.to_thread.run_sync(lambda: _allowed(raw, touch=False)) is not None


async def _pump(websocket: WebSocket, raw: str | None, queue: asyncio.Queue[str]) -> None:
    """Forward notices until the browser goes or the session ends."""
    every = get_settings().ws_recheck_seconds
    next_check = time.monotonic() + every
    gone = asyncio.create_task(_until_disconnect(websocket))
    try:
        while not gone.done():
            getter = asyncio.create_task(queue.get())
            wait = max(0.0, next_check - time.monotonic())
            done, _ = await asyncio.wait(
                {getter, gone}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
            )
            if getter not in done:
                getter.cancel()
            if gone in done:
                return
            message = getter.result() if getter in done else None
            revoked = message is not None and json.loads(message).get("type") == "sessions_revoked"
            if revoked or time.monotonic() >= next_check:
                if not await _still_allowed(raw):
                    await websocket.close(code=UNAUTHORISED)
                    return
                next_check = time.monotonic() + every
            if message is not None and not revoked:
                await websocket.send_text(message)
    finally:
        gone.cancel()


async def chat_socket(websocket: WebSocket) -> None:
    if not _same_origin(websocket.headers.get("origin")):
        await websocket.close(code=FORBIDDEN_ORIGIN)
        return
    raw = websocket.cookies.get(SESSION_COOKIE)
    actor = await anyio.to_thread.run_sync(lambda: _allowed(raw, touch=True))
    if actor is None:
        await websocket.close(code=UNAUTHORISED)
        return
    await websocket.accept()
    async with realtime.get_broker().subscription(realtime.channel(actor.user_id)) as queue:
        await _pump(websocket, raw, queue)


router = APIRouter()
router.routes.append(WebSocketRoute(PATH, chat_socket))
