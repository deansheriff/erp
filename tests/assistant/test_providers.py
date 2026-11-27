"""The provider adapters (ADR 0035), against mock HTTP transports: what each sends, how
replies, refusals and errors are read. Nothing reaches the internet."""

import json
from typing import Any

import anthropic
import httpx
import httpx2
import pytest

from app.modules.assistant.providers import ProviderError
from app.modules.assistant.providers.anthropic_api import AnthropicProvider
from app.modules.assistant.providers.gemini_api import GeminiProvider
from app.modules.assistant.providers.openai_api import OpenAIProvider

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
SEEN: list[Any] = []


def _call(provider: Any) -> Any:
    return provider.generate(system="Be brief.", prompt="Say ok.", schema=SCHEMA, name="kx_test")


def _anthropic(body: dict[str, Any], status: int = 200, model: str = "claude-opus-5-5") -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        SEEN.append(request)
        return httpx2.Response(status, json=body)

    client = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider("sk-ant-test", model, client=client)


def _message(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 4},
    }


def test_anthropic_sends_structured_output_with_the_refusal_fallback() -> None:
    SEEN.clear()
    reply = _call(_anthropic(_message('{"ok": true}')))
    assert reply.data == {"ok": True} and (reply.input_tokens, reply.output_tokens) == (11, 4)
    request = SEEN[-1]
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer sk-ant-test"  # how SDK 2.x sends the key
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000
    assert body["fallbacks"] == "default" and body["system"] == "Be brief."
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}

    SEEN.clear()
    _call(_anthropic(_message('{"ok": true}'), model="claude-haiku-5-5"))
    assert "fallbacks" not in json.loads(SEEN[-1].content)  # only on the models that take it

    refused = _call(_anthropic({**_message(""), "stop_reason": "refusal", "content": []}))
    assert refused.refused and refused.data is None
    with pytest.raises(ProviderError, match="API key"):
        _call(
            _anthropic(
                {"type": "error", "error": {"type": "authentication_error", "message": "x"}}, 401
            )
        )


def _openai(body: dict[str, Any], status: int = 200) -> OpenAIProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        SEEN.append(request)
        return httpx.Response(status, json=body)

    return OpenAIProvider(
        "sk-openai-test", "gpt-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_openai_asks_for_a_strict_json_schema() -> None:
    SEEN.clear()
    reply = _call(
        _openai(
            {
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3},
            }
        )
    )
    assert reply.data == {"ok": True} and reply.input_tokens == 9
    request = SEEN[-1]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-openai-test"
    assert body["response_format"]["json_schema"] == {
        "name": "kx_test",
        "schema": SCHEMA,
        "strict": True,
    }
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    refused = _call(_openai({"choices": [{"message": {"refusal": "No.", "content": None}}]}))
    assert refused.refused
    with pytest.raises(ProviderError, match="model"):
        _call(_openai({"error": {}}, 404))


def _gemini(body: dict[str, Any], status: int = 200) -> GeminiProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        SEEN.append(request)
        return httpx.Response(status, json=body)

    return GeminiProvider(
        "gm-test-key", "gemini-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_gemini_asks_for_json_and_reports_safety_blocks() -> None:
    SEEN.clear()
    reply = _call(
        _gemini(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": '{"ok": '}, {"text": "true}"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
            }
        )
    )
    assert reply.data == {"ok": True} and reply.output_tokens == 2
    request = SEEN[-1]
    body = json.loads(request.content)
    assert request.url.path == "/v1beta/models/gemini-test:generateContent"
    assert request.headers["x-goog-api-key"] == "gm-test-key"
    assert body["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert _call(_gemini({"candidates": [{"finishReason": "SAFETY"}]})).refused
    assert _call(_gemini({"promptFeedback": {"blockReason": "OTHER"}})).refused
    with pytest.raises(ProviderError, match="busy"):
        _call(_gemini({}, 429))
