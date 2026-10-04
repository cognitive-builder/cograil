"""What the Anthropic and Ollama providers share: tool wire names and the prompt."""

from collections.abc import Sequence
from typing import Any

from cograil.domain import Step, Tool
from cograil.errors import ProviderError
from cograil.providers.base import STEP_COMPLETE, Message

# Offered on every call: the structured signal that ends a Step (ADR 0008). Its name is
# reserved, so no Tool may share its wire name.
STEP_COMPLETE_SPEC: dict[str, Any] = {
    "name": STEP_COMPLETE,
    "description": "Call this when the step's work is done. The step ends only through it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "The step's result, for later steps."},
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How confident you are in the result, from 0 to 1.",
            },
        },
        "required": ["output", "confidence"],
    },
}


def wire_name(name: str) -> str:
    """The APIs accept only [a-zA-Z0-9_-] in tool names; Cograil names are dotted."""
    return name.replace(".", "__")


def system_prompt(step: Step) -> str:
    return f"Step {step.number}: {step.name}\n\n{step.instruction}"


def full_system_prompt(step: Step, prefix: str) -> str:
    """The stable `prefix` first, then the Step's own text, for providers without saved context."""
    return f"{prefix}\n\n{system_prompt(step)}" if prefix else system_prompt(step)


def messages(context: Sequence[Message]) -> list[dict[str, str]]:
    listed = [{"role": m.role, "content": m.content} for m in context]
    if not listed or listed[0]["role"] != "user":
        listed.insert(0, {"role": "user", "content": "Carry out the step."})
    return listed


def wire_names(tools: Sequence[Tool]) -> dict[str, str]:
    """Map each wire name back to its tool; two tools sharing a wire name would misroute calls."""
    names: dict[str, str] = {STEP_COMPLETE: STEP_COMPLETE}
    for tool in tools:
        wire = wire_name(tool.name)
        if wire in names:
            raise ProviderError(
                f"Tools {names[wire]!r} and {tool.name!r} share the wire name {wire!r}"
            )
        names[wire] = tool.name
    return names
