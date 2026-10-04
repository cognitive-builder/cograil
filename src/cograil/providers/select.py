"""Pick the Provider a harness names (ADR 0005, 0010)."""

import os

from cograil.domain import Harness
from cograil.providers.anthropic import AnthropicProvider
from cograil.providers.base import Provider
from cograil.providers.ollama import OllamaProvider


def make_provider(harness: Harness, model: str) -> Provider:
    """The harness's provider, with `model` as its fallback; the runner names one per call."""
    if harness.provider == "ollama":
        return OllamaProvider(model)
    return AnthropicProvider(model)


def provider_ready(harness: Harness) -> bool:
    """Whether the harness's provider can be called: Ollama needs no key, Anthropic does."""
    return harness.provider != "anthropic" or bool(os.environ.get("ANTHROPIC_API_KEY"))
