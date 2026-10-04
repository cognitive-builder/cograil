"""Model providers behind one interface."""

from cograil.providers.anthropic import AnthropicProvider
from cograil.providers.base import (
    Message,
    Plan,
    PlannedToolCall,
    Provider,
    Usage,
    resolve_model,
)
from cograil.providers.fake import FakeProvider, scripted

__all__ = [
    "AnthropicProvider",
    "FakeProvider",
    "Message",
    "Plan",
    "PlannedToolCall",
    "Provider",
    "Usage",
    "resolve_model",
    "scripted",
]
