"""FakeProvider: replays scripted Plans so unit tests never touch a real model."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from cograil.domain import Effort, Step, Tool
from cograil.errors import ProviderError
from cograil.providers.base import (
    SCREEN_STEP,
    Message,
    Plan,
    PlannedToolCall,
    StepComplete,
    Usage,
)


class FakeCall:
    """One recorded `plan` call, so tests can assert what the runner offered."""

    def __init__(
        self,
        step: Step,
        context: list[Message],
        tools: list[Tool],
        model: str | None = None,
        effort: Effort | None = None,
        prefix: str = "",
    ) -> None:
        self.step = step
        self.context = context
        self.tools = tools
        self.model = model
        self.effort = effort
        self.prefix = prefix


class FakeProvider:
    """Replays `script` for the Steps' calls, in order, recording each in `calls`.

    The injection screen's calls (`SCREEN_STEP`, injection.py) are answered from `screens`
    instead and recorded in `screened`, so a script holds only the Steps' own plans. Once
    `screens` runs out, the screen is told `none` at no cost: a model that finds nothing.
    """

    def __init__(
        self, script: Sequence[Plan], model: str = "fake-model", screens: Sequence[Plan] = ()
    ) -> None:
        self._script = list(script)
        self._screens = list(screens)
        self.model = model
        self.calls: list[FakeCall] = []
        self.screened: list[FakeCall] = []

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
        made = FakeCall(step, list(context), list(tools), model, effort, prefix)
        if step.name == SCREEN_STEP:
            self.screened.append(made)
            if len(self.screened) > len(self._screens):
                return scripted("none", input_tokens=0, output_tokens=0)
            return self._screens[len(self.screened) - 1]
        self.calls.append(made)
        if len(self.calls) > len(self._script):
            raise ProviderError(f"FakeProvider script exhausted after {len(self._script)} plans")
        return self._script[len(self.calls) - 1]


def scripted(
    text: str = "",
    *tool_calls: PlannedToolCall,
    done: bool = False,
    input_tokens: int = 10,
    output_tokens: int = 5,
    confidence: float | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Plan:
    """Build a Plan for a script without spelling out the usage each time.

    done=True adds the step_complete signal with `text` as the Step's output and `confidence`.
    """
    return Plan(
        text=text,
        tool_calls=list(tool_calls),
        step_complete=StepComplete(output=text, confidence=confidence) if done else None,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        model="fake-model",
        stop_reason="tool_use" if tool_calls else "end_turn",
    )


def load_script(path: Path) -> list[Plan]:
    """Plans from a YAML (or JSON) file: a list of {text, done, tool_calls: [{tool, args}]}.

    For demos and tests of the CLI (`--fake-script`); unknown keys are an error.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProviderError(f"{path}: cannot read script: {exc}") from exc
    if not isinstance(data, list):
        raise ProviderError(f"{path}: a script is a list of plans")
    return [_plan(path, number, item) for number, item in enumerate(data, start=1)]


def _plan(path: Path, number: int, item: Any) -> Plan:
    if not isinstance(item, dict) or not item.keys() <= {"text", "done", "tool_calls"}:
        raise ProviderError(f"{path}: plan {number} must hold only text, done and tool_calls")
    calls = item.get("tool_calls") or []
    if not isinstance(calls, list) or not all(
        isinstance(c, dict) and isinstance(c.get("tool"), str) for c in calls
    ):
        raise ProviderError(f"{path}: plan {number}: tool_calls need a tool name each")
    planned = [
        PlannedToolCall(id=f"call-{number}-{i}", tool=c["tool"], args=c.get("args") or {})
        for i, c in enumerate(calls, start=1)
    ]
    return scripted(str(item.get("text", "")), *planned, done=bool(item.get("done", False)))
