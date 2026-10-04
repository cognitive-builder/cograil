"""The Provider interface: the only way the runner talks to a model (ADR 0005).

A Provider reasons inside one Step. It returns text and structured tool calls plus
token usage; it never executes a tool and never decides whether a call is allowed.
The runner enforces the Step whitelist and the Gates.
"""

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cograil.domain import Colleague, Step, Tool
from cograil.domain import Protocol as ProtocolDef


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
    """The structured signal that ends a Step (ADR 0008); output is passed to later Steps."""

    output: str = ""


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


class Provider(Protocol):
    async def plan(self, step: Step, context: Sequence[Message], tools: Sequence[Tool]) -> Plan:
        """Ask the model what to do next in `step`, offering only `tools`."""
        ...


def resolve_model(protocol: ProtocolDef, colleague: Colleague) -> str:
    """Protocol.model wins; otherwise the Colleague's model_policy."""
    return protocol.model or colleague.model_policy
