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
    resolve_model,
)
from cograil.providers.fake import FakeProvider, scripted

__all__ = [
    "STEP_COMPLETE",
    "AnthropicProvider",
    "FakeProvider",
    "Message",
    "Plan",
    "PlannedToolCall",
    "Provider",
    "StepComplete",
    "Usage",
    "resolve_model",
    "scripted",
]
