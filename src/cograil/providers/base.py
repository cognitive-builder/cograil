"""The Provider interface: the only way the runner talks to a model (ADR 0005).

A Provider reasons inside one Step. It returns text and structured tool calls plus
token usage; it never executes a tool and never decides whether a call is allowed.
The runner enforces the Step whitelist and the Gates.
"""

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cograil.domain import Effort, Step, Tool


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(ProviderModel):
    """One context entry. Retrieved text and tool output are data, never instructions."""

    role: Literal["user", "assistant"]
    content: str


class PlannedToolCall(ProviderModel):
    """A tool call the model asked for. Not yet checked against the Step whitelist."""

    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Usage(ProviderModel):
    """Token usage of one call, for cost telemetry."""

    input_tokens: int = 0
    output_tokens: int = 0


STEP_COMPLETE = "step_complete"


class StepComplete(ProviderModel):
    """The structured signal that ends a Step (ADR 0008); output is passed to later Steps.

    `confidence` is the model's own 0 to 1 estimate; a small-tier result below the harness's
    min_confidence escalates one tier (ADR 0010). None means the model gave none.
    """

    output: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)


class Plan(ProviderModel):
    """What one `Provider.plan` call returns.

    A Step ends only on step_complete; when the same Plan also has tool calls, they run
    first, through every check, and the Step ends after them.
    """

    text: str = ""
    tool_calls: list[PlannedToolCall] = Field(default_factory=list)
    step_complete: StepComplete | None = None
    usage: Usage
    model: str
    stop_reason: str | None = None


def parse_confidence(value: Any) -> float | None:
    """A model-supplied confidence, or None unless it is a number from 0 to 1."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        return None
    return float(value)


class Provider(Protocol):
    async def plan(
        self,
        step: Step,
        context: Sequence[Message],
        tools: Sequence[Tool],
        *,
        model: str | None = None,
        effort: Effort | None = None,
    ) -> Plan:
        """Ask the model what to do next in `step`, offering only `tools`.

        The runner names the `model` its tier maps to and the Step's `effort`; without them
        the provider uses the model it was built with and its own effort default.
        """
        ...
