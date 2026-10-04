"""Ollama provider for local small models (ADR 0010); needs the `ollama` extra.

It runs the models a harness maps under `providers.ollama`, on a laptop or on a DGX Spark,
so a small-tier step can stay on the client's network. The server is `OLLAMA_HOST`
(default http://localhost:11434). Ollama has no effort setting, so `effort` is ignored.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import httpx

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
    full_system_prompt,
    messages,
    wire_name,
    wire_names,
)

DEFAULT_MAX_TOKENS = 4096
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _sdk() -> Any:
    try:
        import ollama
    except ImportError as exc:
        raise ProviderError(
            "the Ollama provider needs the extra: pip install 'cograil[ollama]'"
        ) from exc
    return ollama


def _function(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


def _tool_specs(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    specs = [
        _function(wire_name(t.name), t.description, t.args_schema or _EMPTY_SCHEMA) for t in tools
    ]
    done = STEP_COMPLETE_SPEC
    return [*specs, _function(done["name"], done["description"], done["input_schema"])]


def _to_plan(response: Any, names: dict[str, str], model: str) -> Plan:
    message = response.message
    calls: list[PlannedToolCall] = []
    done: dict[str, Any] | None = None
    for use in message.tool_calls or []:
        args = dict(use.function.arguments)
        if use.function.name == STEP_COMPLETE:
            done = args
        else:  # Ollama gives no call ids; the runner needs one per call
            name = names.get(use.function.name, use.function.name)
            calls.append(PlannedToolCall(id=f"call_{uuid.uuid4().hex[:12]}", tool=name, args=args))
    usage = Usage(
        input_tokens=response.prompt_eval_count or 0, output_tokens=response.eval_count or 0
    )
    complete = None
    if done is not None:
        complete = StepComplete(
            output=str(done.get("output", "")), confidence=parse_confidence(done.get("confidence"))
        )
    return Plan(
        text=message.content or "",
        tool_calls=calls,
        step_complete=complete,
        usage=usage,
        model=model,
        stop_reason=response.done_reason,
    )


class OllamaProvider:
    def __init__(
        self,
        model: str,
        client: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # AsyncClient reads OLLAMA_HOST from the environment; no address lives in the repo.
        self._client = client or _sdk().AsyncClient()

    async def plan(
        self,
        step: Step,
        context: Sequence[Message],
        tools: Sequence[Tool],
        *,
        model: str | None = None,
        effort: Effort | None = None,
        prefix: str = "",
    ) -> Plan:
        names = wire_names(tools)
        chosen = model or self.model
        history = [{"role": "system", "content": full_system_prompt(step, prefix)},
                   *messages(context)]  # fmt: skip
        try:
            response = await self._client.chat(
                model=chosen,
                messages=history,
                tools=_tool_specs(tools),
                options={"num_predict": self.max_tokens},
            )
        except (_sdk().ResponseError, httpx.HTTPError, ConnectionError) as exc:
            raise ProviderError(f"Ollama call failed: {exc}") from exc
        return _to_plan(response, names, chosen)
