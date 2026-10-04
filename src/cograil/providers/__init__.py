"""Model providers behind one interface."""

from cograil.providers.anthropic import AnthropicProvider
from cograil.providers.base import (
    STEP_COMPLETE,
    Message,
    Plan,
    PlannedToolCall,
    Provider,
    StepComplete,
    Usage,
)
from cograil.providers.fake import FakeProvider, scripted
from cograil.providers.ollama import OllamaProvider
from cograil.providers.select import make_provider, provider_ready

__all__ = [
    "STEP_COMPLETE",
    "AnthropicProvider",
    "FakeProvider",
    "Message",
    "OllamaProvider",
    "Plan",
    "PlannedToolCall",
    "Provider",
    "StepComplete",
    "Usage",
    "make_provider",
    "provider_ready",
    "scripted",
]
