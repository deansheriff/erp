"""Model providers for the assistant (ADR 0035): one interface, one adapter each for
Anthropic, OpenAI and Gemini. Every adapter asks for JSON matching a schema and reports
refusals; tests swap in a fake with `set_provider`."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

TIMEOUT_SECONDS = 120.0
MAX_TOKENS = 16000


@dataclass(frozen=True, slots=True)
class Reply:
    data: dict[str, Any] | None  # the JSON reply; None when refused
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


class ProviderError(Exception):
    """A failure to show the person. `message` never includes the provider's raw error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Provider(Protocol):
    def generate(self, *, system: str, prompt: str, schema: dict[str, Any], name: str) -> Reply: ...


Factory = Callable[[str, str, str], Provider]  # (provider, model, api key) -> Provider
_override: Provider | None = None


def set_provider(provider: Provider | None) -> None:
    """Tests use a fake provider; nothing reaches the internet."""
    global _override  # noqa: PLW0603
    _override = provider


def build(provider: str, model: str, api_key: str) -> Provider:
    if _override is not None:
        return _override
    if provider == "anthropic":
        from app.modules.assistant.providers.anthropic_api import AnthropicProvider  # noqa: PLC0415

        return AnthropicProvider(api_key, model)
    if provider == "openai":
        from app.modules.assistant.providers.openai_api import OpenAIProvider  # noqa: PLC0415

        return OpenAIProvider(api_key, model)
    if provider == "gemini":
        from app.modules.assistant.providers.gemini_api import GeminiProvider  # noqa: PLC0415

        return GeminiProvider(api_key, model)
    raise ProviderError("Choose a provider in the assistant settings.")


def http_problem(status: int) -> str:
    """A message for an HTTP status from a provider."""
    if status in (401, 403):
        return "The provider refused the API key. Check it in the assistant settings."
    if status == 404:
        return "The provider doesn't recognise that model. Check it in the assistant settings."
    if status == 429:
        return "The provider is busy or the account's limit is reached. Try again shortly."
    if status >= 500:
        return "The provider had a problem. Try again shortly."
    return "The provider couldn't handle this request."
