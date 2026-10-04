"""Anthropic provider on the official SDK. The model name is configuration (ADR 0005)."""

from collections.abc import Sequence
from typing import Any

import anthropic
from anthropic.types import Message as SdkMessage

from cograil.domain import Effort, Step, Tool
from cograil.errors import ProviderError
from cograil.providers.base import (
    STEP_COMPLETE,
    Message,
    Plan,
    PlannedToolCall,
    StepComplete,
    Usage,
    parse_confidence,
)
from cograil.providers.wire import (
    STEP_COMPLETE_SPEC,
    messages,
    system_prompt,
    wire_name,
    wire_names,
)

__all__ = ["STEP_COMPLETE_SPEC", "AnthropicProvider", "wire_names"]

DEFAULT_MAX_TOKENS = 4096
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _tool_spec(tool: Tool) -> dict[str, Any]:
    return {
        "name": wire_name(tool.name),
        "description": tool.description,
        "input_schema": tool.args_schema or _EMPTY_SCHEMA,
    }


def _complete(done: dict[str, Any]) -> StepComplete:
    return StepComplete(
        output=str(done.get("output", "")), confidence=parse_confidence(done.get("confidence"))
    )


def _to_plan(response: SdkMessage, names: dict[str, str]) -> Plan:
    texts = [b.text for b in response.content if b.type == "text"]
    uses = [b for b in response.content if b.type == "tool_use"]
    calls = [
        PlannedToolCall(id=b.id, tool=names.get(b.name, b.name), args=dict(b.input))
        for b in uses
        if b.name != STEP_COMPLETE
    ]
    done = next((dict(b.input) for b in uses if b.name == STEP_COMPLETE), None)
    usage = Usage(
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
    )
    return Plan(
        text="".join(texts),
        tool_calls=calls,
        step_complete=None if done is None else _complete(done),
        usage=usage,
        model=response.model,
        stop_reason=response.stop_reason,
    )


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # The SDK reads ANTHROPIC_API_KEY from the environment; no key lives in the repo.
        self._client = client or anthropic.AsyncAnthropic()

    async def plan(
        self,
        step: Step,
        context: Sequence[Message],
        tools: Sequence[Tool],
        *,
        model: str | None = None,
        effort: Effort | None = None,
    ) -> Plan:
        names = wire_names(tools)
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt(step),
            "messages": messages(context),
            "tools": [*(_tool_spec(t) for t in tools), STEP_COMPLETE_SPEC],
        }
        if effort is not None:  # the API's effort levels; sent only when a Step or harness asks
            kwargs["extra_body"] = {"output_config": {"effort": effort}}
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic call failed: {exc}") from exc
        return _to_plan(response, names)
