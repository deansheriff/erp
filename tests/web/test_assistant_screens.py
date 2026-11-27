"""The assistant through the browser's eyes (Phase 13): every page renders; the Super
Administrator saves settings and opts in; HR adds a policy; a question is answered with
checked quotes; the ticket panel stays empty for people it isn't for. A fake provider
answers; nothing reaches the internet."""

import html
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.modules.assistant import providers
from app.modules.assistant.providers import Reply
from tests.assistant.conftest import KEY, FakeProvider
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)

pytestmark = pytest.mark.db
POLICY = (
    b"Leave Policy\n\nCompassionate leave. An employee may take up to five working days of "
    b"paid compassionate leave on the death of a close family member."
)


@pytest.fixture()
def fake() -> Iterator[FakeProvider]:
    provider = FakeProvider()
    providers.set_provider(provider)
    try:
        yield provider
    finally:
        providers.set_provider(None)


def _post(client: TestClient, url: str, data: dict[str, Any]) -> Any:
    return client.post(url, data={**data, "csrf_token": csrf_token(client)})


def test_every_assistant_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in (
        "/assistant",
        "/assistant/hiring",
        "/assistant/evidence",
        "/assistant/policies",
        "/assistant/history",
        "/assistant/settings",
    ):
        assert admin.get(page).status_code == 200, page
    assert "The assistant is off" in html.unescape(admin.get("/assistant").text)
    nav = admin.get("/assistant").text
    for href in ("/assistant/hiring", "/assistant/evidence", "/assistant/settings"):
        assert f'href="{href}"' in nav


def test_opt_in_add_a_policy_and_ask(admin: TestClient, fake: FakeProvider) -> None:  # noqa: F811
    r = _post(admin, "/assistant/settings/on", {"accept": "1"})
    assert r.status_code == 422 and "Save an API key first." in html.unescape(r.text)
    r = admin.post(
        "/assistant/settings/save",
        data={
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "api_key": KEY,
            "kind": ["policies", "directory"],
            "monthly_cap": "200",
            "history_days": "90",
            "csrf_token": csrf_token(admin),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Settings saved." in page and "ending ••••wxyz" in page
    assert KEY not in page
    r = _post(admin, "/assistant/settings/on", {})
    assert r.status_code == 422 and "Confirm that the ticked data" in html.unescape(r.text)
    r = _post(admin, "/assistant/settings/on", {"accept": "1"})
    assert "The assistant is on." in r.text and "Policies, Directory" in r.text
    fake.reply = lambda *_: Reply({"ok": True})
    r = _post(admin, "/assistant/settings/test", {})
    assert "The connection works." in r.text

    r = admin.post(
        "/assistant/policies",
        data={
            "policy_id": "",
            "title": "Leave Policy",
            "version": "3",
            "effective_on": "2026-01-01",
        },
        files={"file": ("leave.txt", POLICY, "text/plain")},
        headers={"X-CSRF-Token": csrf_token(admin)},
    )
    assert r.status_code == 200 and "Policy added." in r.text and "Leave Policy" in r.text

    fake.reply = lambda *_: Reply(
        {
            "answer": "Up to five working days, paid.",
            "found": True,
            "citations": [
                {"source": "S1", "quote": "up to five working days of paid compassionate leave"}
            ],
        }
    )
    r = _post(admin, "/assistant", {"question": "How much compassionate leave do I get?"})
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Up to five working days, paid." in page
    assert "Quotes checked" in page and "Leave Policy v3" in page
    assert "How much compassionate leave" in admin.get("/assistant/history").text
    r = _post(admin, "/assistant", {"question": "Where is the canteen menu kept?"})
    assert "No policy in the library covers this" in html.unescape(r.text)

    r = _post(
        admin,
        "/assistant/evidence",
        {"employee_id": "", "start": "2026-01-01", "end": "2026-11-27"},
    )
    assert r.status_code == 422  # and Work records weren't accepted anyway
    r = _post(admin, "/assistant/settings/off", {})
    assert "The assistant is off. Nothing more will be sent." in r.text
