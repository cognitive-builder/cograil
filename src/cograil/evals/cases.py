"""Eval cases: the JSONL golden set `cograil eval` runs (issue #30).

One case per line, every key checked (unknown keys are an error):

- `id`, `protocol`, `principal`, `prompt`: which Protocol is run, by whom, asked what.
- `smoke`: true puts the case in the small-tier smoke subset.
- `scripted_only`: true for a case whose model misbehaves on purpose (it calls a Tool its Step
  does not list, never signals step_complete, and so on). Only the FakeProvider can play that
  model, so the live levels leave the case out.
- `script`: the FakeProvider's plans, `[{text, done, confidence, tool_calls: [{tool, args}]}]`;
  the live levels ignore it.
- `decisions`: how each gate the Run meets is decided, in order, by its approver.
- `expect`: the `status` the Run ends in (a RunStatus, or `denied` when the audience check
  refuses the principal before any Run starts), the typed `error` it failed with if any, its
  `tool_calls` and its `gates` (the Approvals it asked for).

Tool calls and gates are compared in order and in number. Their expected `args` are key args:
each must equal the recorded value, and args the case does not name are not compared. A `step`
is compared only when the case gives one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cograil.domain import Approval, RunStatus, ToolCall
from cograil.errors import EvalError
from cograil.providers.base import Plan, PlannedToolCall
from cograil.providers.fake import scripted

DENIED = "denied"
_MISSING = object()


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptedCall(EvalModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ScriptedPlan(EvalModel):
    """One plan the FakeProvider answers with; `done` adds the step_complete signal."""

    text: str = ""
    done: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1)
    tool_calls: list[ScriptedCall] = Field(default_factory=list)


class ExpectedCall(EvalModel):
    """A recorded tool call or gate: its Tool, its key args and, if given, its Step."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    step: int | None = None


class Expectation(EvalModel):
    status: RunStatus | Literal["denied"]
    error: str | None = None
    tool_calls: list[ExpectedCall] = Field(default_factory=list)
    gates: list[ExpectedCall] = Field(default_factory=list)


class EvalCase(EvalModel):
    id: str = Field(min_length=1)
    protocol: str
    principal: str
    prompt: str = Field(min_length=1)
    smoke: bool = False
    scripted_only: bool = False
    script: list[ScriptedPlan] = Field(default_factory=list)
    decisions: list[Literal["approved", "declined"]] = Field(default_factory=list)
    expect: Expectation

    def plans(self) -> list[Plan]:
        """The script as the Plans a FakeProvider replays."""
        return [_plan(self.id, number, item) for number, item in enumerate(self.script, start=1)]


def _plan(case: str, number: int, item: ScriptedPlan) -> Plan:
    calls = [
        PlannedToolCall(id=f"{case}-{number}-{i}", tool=c.tool, args=c.args)
        for i, c in enumerate(item.tool_calls, start=1)
    ]
    return scripted(item.text, *calls, done=item.done, confidence=item.confidence)


def load_cases(path: Path) -> list[EvalCase]:
    """The cases of a JSONL file; blank lines are skipped. Raises EvalError naming the line."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvalError(f"{path}: cannot read cases: {exc}") from exc
    cases: list[EvalCase] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = EvalCase.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise EvalError(f"{path}:{number}: invalid case: {exc}") from exc
        if any(seen.id == case.id for seen in cases):
            raise EvalError(f"{path}:{number}: case id {case.id!r} is used twice")
        cases.append(case)
    if not cases:
        raise EvalError(f"{path}: no cases")
    return cases


def mismatches(
    expect: Expectation,
    status: str,
    error: str | None,
    calls: Sequence[ToolCall],
    gates: Sequence[Approval],
) -> list[str]:
    """How a Run's outcome differs from what the case expects; empty when it passes."""
    found: list[str] = []
    if status != expect.status:
        found.append(f"status: expected {expect.status}, got {status}")
    if error != expect.error:
        found.append(f"error: expected {expect.error}, got {error}")
    found += _compared("tool call", expect.tool_calls, [(c.step, c.tool, c.args) for c in calls])
    found += _compared("gate", expect.gates, [(a.step, a.tool, a.args) for a in gates])
    return found


def _compared(
    kind: str, expected: Sequence[ExpectedCall], actual: Sequence[tuple[int, str, dict[str, Any]]]
) -> list[str]:
    wanted, got = [e.tool for e in expected], [tool for _, tool, _ in actual]
    if wanted != got:
        return [f"{kind}s: expected {wanted}, got {got}"]
    found: list[str] = []
    for number, (each, (step, tool, args)) in enumerate(zip(expected, actual, strict=True), 1):
        if each.step is not None and each.step != step:
            found.append(f"{kind} {number} ({tool}): step expected {each.step}, got {step}")
        for key, value in each.args.items():
            if args.get(key, _MISSING) != value:
                shown = args.get(key, "<missing>")
                found.append(f"{kind} {number} ({tool}): {key} expected {value!r}, got {shown!r}")
    return found
