"""FakeProvider: replays scripted Plans so unit tests never touch a real model."""

from collections.abc import Sequence

from cograil.domain import Step, Tool
from cograil.errors import ProviderError
from cograil.providers.base import Message, Plan, PlannedToolCall, Usage


class FakeCall:
    """One recorded `plan` call, so tests can assert what the runner offered."""

    def __init__(self, step: Step, context: list[Message], tools: list[Tool]) -> None:
        self.step = step
        self.context = context
        self.tools = tools


class FakeProvider:
    def __init__(self, script: Sequence[Plan], model: str = "fake-model") -> None:
        self._script = list(script)
        self.model = model
        self.calls: list[FakeCall] = []

    async def plan(self, step: Step, context: Sequence[Message], tools: Sequence[Tool]) -> Plan:
        self.calls.append(FakeCall(step, list(context), list(tools)))
        if len(self.calls) > len(self._script):
            raise ProviderError(f"FakeProvider script exhausted after {len(self._script)} plans")
        return self._script[len(self.calls) - 1]


def scripted(
    text: str = "",
    *tool_calls: PlannedToolCall,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> Plan:
    """Build a Plan for a script without spelling out the usage each time."""
    return Plan(
        text=text,
        tool_calls=list(tool_calls),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
        stop_reason="tool_use" if tool_calls else "end_turn",
    )
