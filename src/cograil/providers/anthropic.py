"""Anthropic provider on the official SDK. The model name is configuration (ADR 0005)."""

from collections.abc import Sequence
from typing import Any

import anthropic
from anthropic.types import Message as SdkMessage

from cograil.domain import Colleague, Step, Tool
from cograil.domain import Protocol as ProtocolDef
from cograil.errors import ProviderError
from cograil.providers.base import Message, Plan, PlannedToolCall, Usage, resolve_model

DEFAULT_MAX_TOKENS = 4096
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _tool_spec(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.args_schema or _EMPTY_SCHEMA,
    }


def _system_prompt(step: Step) -> str:
    return f"Step {step.number}: {step.name}\n\n{step.instruction}"


def _messages(context: Sequence[Message]) -> list[dict[str, str]]:
    messages = [{"role": m.role, "content": m.content} for m in context]
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "Carry out the step."})
    return messages


def _to_plan(response: SdkMessage) -> Plan:
    texts = [b.text for b in response.content if b.type == "text"]
    calls = [
        PlannedToolCall(id=b.id, tool=b.name, args=dict(b.input))
        for b in response.content
        if b.type == "tool_use"
    ]
    usage = Usage(
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
    )
    return Plan(
        text="".join(texts),
        tool_calls=calls,
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

    @classmethod
    def for_protocol(
        cls,
        protocol: ProtocolDef,
        colleague: Colleague,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> "AnthropicProvider":
        return cls(resolve_model(protocol, colleague), client=client)

    async def plan(self, step: Step, context: Sequence[Message], tools: Sequence[Tool]) -> Plan:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": _system_prompt(step),
            "messages": _messages(context),
        }
        if tools:
            kwargs["tools"] = [_tool_spec(t) for t in tools]
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic call failed: {exc}") from exc
        return _to_plan(response)
