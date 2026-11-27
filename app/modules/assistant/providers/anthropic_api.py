"""Claude through the official Anthropic SDK (ADR 0035): structured JSON output, the
server-side refusal fallback on the models that take it, and refusals reported."""

import json
from typing import Any

import anthropic

from app.modules.assistant.providers import (
    MAX_TOKENS,
    TIMEOUT_SECONDS,
    ProviderError,
    Reply,
    http_problem,
)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = frozenset(
    {
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
    }
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, client: anthropic.Anthropic | None = None) -> None:
        self.model = model
        self.client = client or anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=2
        )

    def _create(self, **request: Any) -> Any:
        if self.model in FALLBACK_MODELS:
            return self.client.beta.messages.create(
                **request, betas=[FALLBACK_BETA], fallbacks="default"
            )
        return self.client.messages.create(**request)

    def generate(self, *, system: str, prompt: str, schema: dict[str, Any], name: str) -> Reply:
        del name  # Anthropic's schema output needs no name
        try:
            response = self._create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIStatusError as exc:
            raise ProviderError(http_problem(exc.status_code)) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ProviderError("The provider couldn't be reached. Try again shortly.") from exc
        usage = response.usage
        tokens = (int(usage.input_tokens or 0), int(usage.output_tokens or 0))
        if response.stop_reason == "refusal":
            return Reply(None, *tokens, refused=True)
        if response.stop_reason == "max_tokens":
            raise ProviderError("The reply was too long and was cut off. Try a narrower request.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("The provider's reply couldn't be read.") from exc
        return Reply(data if isinstance(data, dict) else None, *tokens)
