"""Request and response shapes of the web service, as served in the OpenAPI document.

They are views of the domain models: a Run, its Steps, ToolCalls and Gates (Approvals). An
Approval's token is shown only to its approver (`ApprovalView`) and to the Run's principal as
the pointer of a paused Run (`PendingApproval`); a Run's detail lists its gates without it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cograil.domain import Approval, AuditEvent, Run, RunStatus, Step, ToolCall

MAX_MESSAGE_CHARS = 8000


class Request(BaseModel):
    """Base of every body the service accepts: unknown fields are refused, so a body can never
    carry a value the endpoint does not read (the decider of an Approval among them)."""

    model_config = ConfigDict(extra="forbid")


class ChatRequest(Request):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class DecisionRequest(Request):
    decision: Literal["approved", "declined"]


class RunSummary(BaseModel):
    """One Run as a row: who started it, where it stands, and what it cost so far."""

    id: str
    colleague: str
    protocol: str
    principal_id: str = Field(description="The Principal who started the Run.")
    status: RunStatus
    cost_usd: float
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: Run) -> RunSummary:
        return cls(
            id=run.id,
            colleague=run.colleague,
            principal_id=run.principal_id,
            protocol=run.protocol,
            status=run.status,
            cost_usd=run.cost_usd,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class StepView(BaseModel):
    """`done` up to the Run's cursor, `current` is the Step the Run is at or stopped on."""

    number: int
    name: str
    tools: list[str]
    state: Literal["done", "current", "pending"]
    output: str | None = None

    @classmethod
    def of(cls, step: Step, run: Run) -> StepView:
        record = run.context.get("steps", {}).get(str(step.number), {})
        state = "done" if step.number <= run.cursor else "pending"
        if step.number == run.cursor + 1 and run.status is not RunStatus.completed:
            state = "current"
        output = record.get("output")
        return cls(
            number=step.number,
            name=step.name,
            tools=step.tools,
            state=state,
            output=output if isinstance(output, str) else None,
        )


class GateView(BaseModel):
    step: int
    tool: str
    args: dict[str, Any]
    approver: str
    decision: Literal["pending", "approved", "declined", "expired"]
    decided_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def of(cls, approval: Approval) -> GateView:
        return cls(**approval.model_dump(include=set(cls.model_fields)))


class RunDetail(RunSummary):
    protocol_version: int
    protocol_changed: bool = Field(
        description="The Workspace has moved on: the Protocol is gone or at another version, "
        "so the Run's Steps cannot be shown and `steps` is empty."
    )
    harness_version: str
    cursor: int
    steps: list[StepView]
    calls: list[ToolCall]
    gates: list[GateView]


class PendingApproval(BaseModel):
    token: str
    approver: str
    tool: str
    step: int


class RunOutcome(BaseModel):
    """Where a Run stands after a chat or a decision; `awaiting` lists the gates it waits on:
    every one to the Run's principal, only their own to a deciding approver. `output` is the
    Run's final answer once it is completed, null until then."""

    run: RunSummary
    output: str | None = Field(
        default=None, description="The Run's final output once it is completed; null until then."
    )
    awaiting: list[PendingApproval]


class ApprovalView(BaseModel):
    """What an approver sees before deciding: the call the gate holds, and for whom."""

    token: str
    decision: Literal["pending", "approved", "declined", "expired"]
    approver: str
    requested_by: str
    run_id: str
    colleague: str
    protocol: str
    step: int
    step_name: str | None
    tool: str
    args: dict[str, Any]
    expires_at: datetime | None
    decided_at: datetime | None


class AuditPage(BaseModel):
    items: list[AuditEvent]
    limit: int
    offset: int
    next_offset: int | None = Field(
        description="Pass as `offset` for the next page; null at the end."
    )
