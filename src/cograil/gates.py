"""Gates the runner enforces from Tool metadata, never from prompt text (ADR 0002).

A scope=write Tool with confirm_before_write runs only against an approved, unspent
Approval for the same Run, step, Tool and arguments, and each Approval authorises one call:
`RunStore.spend_approval` lets it through once, atomically, and a `gate.spent` AuditEvent
records it. `require_approval` is the fail-closed core and raises GateRequired.

Where no such Approval exists the runner pauses instead of failing: `Gates.pause` creates a
pending Approval, saves the Step's progress (turn, messages, tool results and the plan
waiting at the gate) in `Run.context["paused"]`, and sets the Run awaiting_approval.
`Gates.resume` decides the Approval once, so of two racing resumes only one goes on. Only
the Approval's approver may decide it, and never the Run's own principal: any other decider
raises ApprovalNotAllowed and changes nothing, and the decider is written on the
`gate.resumed` or `run.escalated` AuditEvent. An approved Approval resumes the Run exactly
at the paused Step; a declined one, or one past its expires_at, escalates the Run to the
Colleague's escalation_contact, as does a Tool reaching its FailureThreshold
(`Gates.escalate`) or a Step hitting a loop bound (`Gates.bounded`, ADR 0008). An Approval
expires after harness.yaml's approvals.timeout_hours.

Interim rules until their issues land: the approver is the Colleague's escalation_contact
(approver routing from decision tables needs the decision table evaluator and a directory
lookup from tier to principal), and escalating records the Run's status and a
`run.escalated` AuditEvent naming the contact; delivering it is the channels' job.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cograil.domain import Approval, AuditEvent, Colleague, Run, RunStatus, Tool
from cograil.errors import (
    ApprovalNotAllowed,
    ApprovalNotSpendable,
    GateRequired,
    LoopBudgetExceeded,
    RunNotPaused,
)
from cograil.observability import log_event
from cograil.providers.base import Message, PlannedToolCall, StepComplete
from cograil.registry import CallContext
from cograil.store import RunStore

Clock = Callable[[], datetime]
GateAuditKind = Literal[
    "gate.paused", "gate.resumed", "gate.spent", "loop.bounded", "run.escalated"
]
EscalationReason = Literal[
    "approval_declined", "approval_expired", "failure_threshold", "loop_budget_exceeded"
]


def needs_approval(tool: Tool) -> bool:
    return tool.scope == "write" and tool.confirm_before_write


async def require_approval(
    store: RunStore,
    ctx: CallContext,
    tool: Tool,
    args: Mapping[str, Any],
    claimed: Collection[str],
) -> Approval | None:
    """An approved, unspent Approval for this call that is not in `claimed`; None when no
    gate applies. Raises GateRequired when the Tool is gated and no such Approval exists.
    """
    if not needs_approval(tool):
        return None
    for approval in await store.list_approvals(ctx.run_id):
        if (
            approval.decision == "approved"
            and approval.spent_at is None
            and approval.token not in claimed
            and approval.step == ctx.step
            and approval.tool == tool.name
            and approval.args == dict(args)
        ):
            return approval
    raise GateRequired(f"{tool.name}: step {ctx.step} has no approved Approval for these args")


class StepProgress(BaseModel):
    """Where a Step stands between turns; saved at a gate so a resume continues there."""

    model_config = ConfigDict(extra="forbid")

    step: int
    turn: int = 0
    messages: list[Message] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)
    planned: list[PlannedToolCall] = Field(default_factory=list)
    tokens: int = 0
    completing: StepComplete | None = None  # the planned calls' Plan also ended the Step
    token: str | None = None


def paused_progress(run: Run, step: int) -> StepProgress | None:
    """The progress saved when the Run paused inside `step`, if it did."""
    saved = run.context.get("paused")
    if saved is None or saved.get("step") != step:
        return None
    return StepProgress.model_validate(saved)


class Gates:
    """Pause, resume, spend and escalate for the Runs of one Colleague."""

    def __init__(
        self,
        store: RunStore,
        colleague: Colleague,
        *,
        timeout: timedelta,
        clock: Clock,
    ) -> None:
        self._store = store
        self._colleague = colleague
        self._timeout = timeout
        self._clock = clock

    async def pause(
        self, run: Run, tool: Tool, args: Mapping[str, Any], progress: StepProgress
    ) -> Run:
        """Ask for an Approval of this call and park the Run awaiting_approval."""
        approval = Approval(
            token=secrets.token_urlsafe(16),
            run_id=run.id,
            step=progress.step,
            tool=tool.name,
            args=dict(args),
            approver=self._colleague.escalation_contact,
            expires_at=self._clock() + self._timeout,
        )
        await self._store.create_approval(approval)
        paused = progress.model_copy(update={"token": approval.token}).model_dump(mode="json")
        context = {**run.context, "paused": paused}
        run = await self._save(run, status=RunStatus.awaiting_approval, context=context)
        detail = {**_about(approval), "approver": approval.approver}
        await self._audit(run, "gate.paused", {**detail, "expires_at": approval.expires_at})
        log_event("gate.paused", step=approval.step, tool=approval.tool)
        return run

    async def spend(self, run: Run, approval: Approval) -> None:
        """Let the one call an Approval authorises through; losing a race raises GateRequired."""
        try:
            await self._store.spend_approval(approval.token, self._clock())
        except ApprovalNotSpendable as exc:
            raise GateRequired(f"{approval.tool}: its Approval is already spent") from exc
        await self._audit(run, "gate.spent", _about(approval))

    async def resume(
        self, token: str, decision: Literal["approved", "declined"], decider: str
    ) -> Run:
        """Decide the Approval the Run is paused on: running again, or escalated.

        Raises ApprovalNotAllowed, changing nothing, unless `decider` is the Approval's
        approver and not the Run's own principal.
        """
        approval, run = await self._paused_on(token)
        if decider != approval.approver or decider == run.principal_id:
            log_event("gate.refused", logging.WARNING, run_id=run.id, step=approval.step,
                      tool=approval.tool, decider=decider)  # fmt: skip
            raise ApprovalNotAllowed(f"{decider} may not decide the Approval for {approval.tool}")
        if self._overdue(approval):
            return await self._decide(run, approval, "expired", decider)
        return await self._decide(run, approval, decision, decider)

    async def expire(self, token: str) -> Run:
        """Escalate the Run if its Approval is past expires_at; otherwise change nothing."""
        approval, run = await self._paused_on(token)
        if not self._overdue(approval):
            return run
        return await self._decide(run, approval, "expired", None)

    async def escalate(self, run: Run, reason: EscalationReason, detail: dict[str, Any]) -> Run:
        """Stop the Run and hand it to the Colleague's escalation_contact."""
        contact = self._colleague.escalation_contact
        run = await self._save(run, status=RunStatus.escalated)
        await self._audit(run, "run.escalated", {"reason": reason, "contact": contact, **detail})
        log_event("run.escalated", logging.WARNING, run_id=run.id, reason=reason, contact=contact)
        return run

    async def bounded(self, run: Run, step: int, exc: LoopBudgetExceeded) -> Run:
        """Escalate a Run whose Step hit a loop bound; never continue, never fail silently."""
        detail = {"step": step, "bound": exc.bound, "limit": exc.limit, "used": exc.used}
        await self._audit(run, "loop.bounded", detail)
        log_event("loop.bounded", logging.WARNING, run_id=run.id, step=step, bound=exc.bound)
        return await self.escalate(run, "loop_budget_exceeded", {**detail, "error": str(exc)})

    async def _paused_on(self, token: str) -> tuple[Approval, Run]:
        approval = await self._store.get_approval(token)
        run = await self._store.get_run(approval.run_id)
        paused = run.context.get("paused") or {}
        if run.status is not RunStatus.awaiting_approval or paused.get("token") != token:
            raise RunNotPaused(f"run {run.id} is not paused on this Approval")
        return approval, run

    def _overdue(self, approval: Approval) -> bool:
        return approval.expires_at is not None and self._clock() >= approval.expires_at

    async def _decide(
        self,
        run: Run,
        approval: Approval,
        decision: Literal["approved", "declined", "expired"],
        decider: str | None,
    ) -> Run:
        """`decider` is None when the Approval timed out with nobody deciding it."""
        # decide_approval succeeds once per Approval, so only one racing resume gets past it.
        approval = await self._store.decide_approval(approval.token, decision, self._clock())
        detail = {**_about(approval), "approver": approval.approver, "decided_by": decider}
        if decision != "approved":
            declined = decision == "declined"
            reason: EscalationReason = "approval_declined" if declined else "approval_expired"
            return await self.escalate(run, reason, detail)
        run = await self._save(run, status=RunStatus.running)
        await self._audit(run, "gate.resumed", detail)
        log_event("gate.resumed", run_id=run.id, step=approval.step, tool=approval.tool)
        return run

    async def _save(self, run: Run, **changes: Any) -> Run:
        run = run.model_copy(update={**changes, "updated_at": self._clock()})
        await self._store.update_run(run)
        return run

    async def _audit(self, run: Run, kind: GateAuditKind, detail: dict[str, Any]) -> None:
        await self._store.append_audit_event(
            AuditEvent(run_id=run.id, at=self._clock(), principal_id=run.principal_id,
                       kind=kind, detail=detail)
        )  # fmt: skip


def _about(approval: Approval) -> dict[str, Any]:
    """What a gate AuditEvent says about its Approval."""
    return {"step": approval.step, "tool": approval.tool, "token": approval.token}


def utc_now() -> datetime:
    return datetime.now(UTC)
