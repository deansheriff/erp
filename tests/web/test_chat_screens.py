"""Chat through the browser's eyes (Phase 12): every chat page renders; a direct message
with an attachment, unread counts, reactions, pins and deletes through htmx; a
non-member gets "not found"; and the WebSocket checks origin and session, delivers a
notice when a message arrives, and closes when the session is revoked."""

import html
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.core.db import session_scope
from app.identity.models import User
from app.modules.chat.models import Message
from tests.conftest import csrf_token
from tests.files.conftest import png
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _add_and_invite

pytestmark = pytest.mark.db
ORIGIN = {"origin": "http://localhost:8000"}


def _post(client: TestClient, url: str, data: dict[str, Any] | None = None, **headers: str) -> Any:
    return client.post(
        url, data={**(data or {}), "csrf_token": csrf_token(client)}, headers=headers or None
    )


def _hx(client: TestClient) -> dict[str, str]:
    return {"HX-Request": "true", "X-CSRF-Token": csrf_token(client)}


def _user_id(email: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(User.id).where(User.email == email)).one())


def _name(email: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(User.full_name).where(User.email == email)).one())


def _dm(admin: TestClient, email: str = "amaka@adeyemiholdings.ng") -> str:  # noqa: F811
    r = _post(admin, "/chat/new/direct", {"person_id": _user_id(email)})
    assert r.status_code == 200, r.text[:300]
    return str(r.url).rstrip("/").rsplit("/", 1)[-1]


def test_every_chat_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in ("/chat", "/chat/new", "/chat/settings"):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/chat").text
    for href in ("/chat/new", "/chat/settings"):
        assert f'href="{href}"' in nav
    r = _post(
        admin,
        "/chat/settings",
        {"edit_minutes": "15", "email_after_minutes": "30", "retention_days": "7"},
    )
    assert r.status_code == 422 and "from 30" in html.unescape(r.text)
    r = _post(admin, "/chat/new/group", {"title": "", "x": "1"})
    assert r.status_code == 422 and "Give the group a name." in r.text


def test_a_direct_message_through_the_browser(admin: TestClient) -> None:  # noqa: F811
    _, amaka = _add_and_invite(admin)
    name = _name("amaka@adeyemiholdings.ng")  # the name they gave their account
    cid = _dm(admin)
    r = admin.post(
        f"/chat/{cid}/messages",
        data={"body": f"Hello @{name.split()[0]}, the October statement"},
        files={"file": ("statement.png", png(), "image/png")},
        headers=_hx(admin),
    )
    assert r.status_code == 204 and r.headers["HX-Trigger"] == "chat-sent"
    r = admin.post(
        f"/chat/{cid}/messages",
        data={"body": "   "},
        files={"file": ("", b"", "application/octet-stream")},
        headers=_hx(admin),
    )
    assert r.headers.get("HX-Retarget") == "#chat-composer-error"
    assert "Write a message or attach a file." in r.text

    listing = html.unescape(amaka.get("/chat").text)
    assert 'aria-label="1 unread"' in listing and "statement" in listing
    page = html.unescape(amaka.get(f"/chat/{cid}").text)
    assert "statement.png" in page and f"@{name.split()[0]}</span>" in page and "New" in page
    assert 'aria-label="1 unread"' not in html.unescape(amaka.get("/chat").text)
    assert f"Seen by {name}" in admin.get(f"/chat/{cid}/receipt").text

    with session_scope() as db:
        mid = str(db.scalars(select(Message.id).where(Message.conversation_id == cid)).one())
    base = f"/chat/{cid}/messages/{mid}"
    r = amaka.post(f"{base}/react", data={"emoji": "👍"}, headers=_hx(amaka))
    assert r.status_code == 200 and "👍 1" in r.text and f'id="msg-{mid}"' in r.text
    r = amaka.post(f"{base}/pin", headers=_hx(amaka))
    assert f"Pinned by {name}" in r.text
    assert "Pinned:" in admin.get(f"/chat/{cid}/pins").text
    r = amaka.post(f"{base}/delete", headers=_hx(amaka))
    assert "only delete your own" in html.unescape(r.text)
    r = admin.post(f"{base}/delete", headers=_hx(admin))
    assert "Message deleted" in r.text and "statement.png" not in r.text
    assert "Message deleted" in amaka.get(f"/chat/{cid}/messages?after=").text

    group = _post(
        admin,
        "/chat/new/group",
        {"title": "Leadership", f"member_{_user_id('amaka@adeyemiholdings.ng')}": "1"},
    )
    assert "Group created." in group.text
    gid = str(group.url).split("/chat/", 1)[1].split("?", 1)[0]
    assert "Leadership" in amaka.get(f"/chat/{gid}/details").text
    r = _post(amaka, f"/chat/{gid}/details/leave")
    assert r.status_code == 200 and str(r.url).endswith("/chat")
    assert amaka.get(f"/chat/{gid}").status_code == 404  # no longer a member


def test_the_socket_checks_origin_and_session_and_closes_on_revocation(
    admin: TestClient,  # noqa: F811
) -> None:
    _, amaka = _add_and_invite(admin)
    cid = _dm(admin)
    for headers in ({}, {"origin": "https://evil.example"}):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            admin.websocket_connect("/ws/chat", headers=headers) as ws,
        ):
            ws.receive_text()
        assert refused.value.code == 4403
    anonymous = TestClient(admin.app, base_url="http://localhost:8000")
    with (
        pytest.raises(WebSocketDisconnect) as nobody,
        anonymous.websocket_connect("/ws/chat", headers=ORIGIN) as ws,
    ):
        ws.receive_text()
    assert nobody.value.code == 4401

    # The test client doesn't send its cookie jar on a WebSocket handshake; browsers do.
    signed_in = {**ORIGIN, "cookie": f"kx_session={admin.cookies.get('kx_session')}"}
    with admin.websocket_connect("/ws/chat", headers=signed_in) as ws:
        r = amaka.post(
            f"/chat/{cid}/messages",
            data={"body": "Are you there?"},
            files={"file": ("", b"", "application/octet-stream")},
            headers=_hx(amaka),
        )
        assert r.status_code == 204
        notice = ws.receive_json()
        assert notice == {"type": "message", "conversation": cid, "message": notice["message"]}
        assert "Are you there?" not in str(notice)
        _post(admin, "/logout")
        code = None
        for _ in range(5):  # another notice may come first; the close must follow
            try:
                ws.receive_json()
            except WebSocketDisconnect as exc:
                code = exc.code
                break
        assert code == 4401
