"""OpenAI's Chat Completions API over REST (ADR 0035): a strict JSON schema response
format; refusals reported."""

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

URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        self.api_key, self.model = api_key, model
        self.client = client  # tests inject one; otherwise one per request

    def _post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        if self.client is not None:
            return self.client.post(url, json=json, headers=headers)
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            return client.post(url, json=json, headers=headers)

    def generate(self, *, system: str, prompt: str, schema: dict[str, Any], name: str) -> Reply:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
            "max_completion_tokens": MAX_TOKENS,
        }
        try:
            response = self._post(
                URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError("The provider couldn't be reached. Try again shortly.") from exc
        if response.status_code != 200:
            raise ProviderError(http_problem(response.status_code))
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("The provider's reply couldn't be read.") from exc
        usage = payload.get("usage") or {}
        tokens = (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            return Reply(None, *tokens, refused=True)
        if choice.get("finish_reason") == "length":
            raise ProviderError("The reply was too long and was cut off. Try a narrower request.")
        try:
            data = json.loads(message.get("content") or "")
        except json.JSONDecodeError as exc:
            raise ProviderError("The provider's reply couldn't be read.") from exc
        return Reply(data if isinstance(data, dict) else None, *tokens)
