"""Google Gemini's generateContent API over REST (ADR 0035): a JSON response with a
schema; safety blocks reported as refusals."""

import json
from typing import Any

import httpx

from app.modules.assistant.providers import (
    MAX_TOKENS,
    TIMEOUT_SECONDS,
    ProviderError,
    Reply,
    http_problem,
)

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BLOCKED = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


class GeminiProvider:
    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        self.api_key, self.model = api_key, model
        self.client = client  # tests inject one; otherwise one per request

    def _post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        if self.client is not None:
            return self.client.post(url, json=json, headers=headers)
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            return client.post(url, json=json, headers=headers)

    def generate(self, *, system: str, prompt: str, schema: dict[str, Any], name: str) -> Reply:
        del name
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": MAX_TOKENS,
            },
        }
        try:
            response = self._post(
                URL.format(model=self.model),
                json=body,
                headers={"x-goog-api-key": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise ProviderError("The provider couldn't be reached. Try again shortly.") from exc
        if response.status_code != 200:
            raise ProviderError(http_problem(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("The provider's reply couldn't be read.") from exc
        usage = payload.get("usageMetadata") or {}
        tokens = (
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
        )
        candidates = payload.get("candidates") or []
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        if blocked or not candidates or candidates[0].get("finishReason") in BLOCKED:
            return Reply(None, *tokens, refused=True)
        if candidates[0].get("finishReason") == "MAX_TOKENS":
            raise ProviderError("The reply was too long and was cut off. Try a narrower request.")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        try:
            data = json.loads("".join(p.get("text", "") for p in parts))
        except json.JSONDecodeError as exc:
            raise ProviderError("The provider's reply couldn't be read.") from exc
        return Reply(data if isinstance(data, dict) else None, *tokens)
