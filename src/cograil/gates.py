"""Gates the runner enforces from Tool metadata, never from prompt text (ADR 0002).

A scope=write Tool with confirm_before_write runs only against an approved, unspent
Approval for the same Run, step, Tool and arguments, and each Approval authorises one call:
`RunStore.spend_approval` lets it through once, atomically, and a `gate.spent` AuditEvent
written in the same store transaction records it. `require_approval` is the fail-closed
core and raises GateRequired.

Where no such Approval exists the runner pauses instead of failing: `Gates.pause` creates a
pending Approval, saves the Step's progress (turn, messages, tool results and the plan
waiting at the gate) in `Run.context["paused"]`, sets the Run awaiting_approval and writes
the `gate.paused` AuditEvent, all in one store transaction.
`Gates.resume` decides the Approval once, so of two racing resumes only one goes on, and
saves the Run and writes the `gate.resumed` or `run.escalated` AuditEvent in the same store
transaction, so a crash cannot leave one without the others. Only the Approval's approver
may decide it, and never the Run's own principal: any other decider raises
ApprovalNotAllowed and leaves the Run and the Approval as they were (principal ids are
compared normalised, so a spelling cannot slip past; cograil.identity),
with a `gate.refused` AuditEvent whose principal is that decider, the Run's principal in its
detail; the decider is written on the `gate.resumed` or `run.escalated` AuditEvent. An empty
decider is refused with ApprovalNotAllowed before anything is audited. The approver's
approval of a Run started under another harness or tool pack (run_versions.py) is refused
too, with ToolPackChanged or HarnessChanged and a `gate.refused` AuditEvent whose reason is
`run_version_changed`; the approver may still decline it (issue #97). A gate whose
approver would be the Run's own principal, whom nobody else may stand in for, escalates at
pause time instead of waiting out the timeout, with the attempted call's args in its
`run.escalated` detail and the Step's progress in `Run.context["paused"]` (#107). An
approved Approval resumes the Run exactly at the paused Step; a declined one, or one past
its expires_at, escalates the Run to the Colleague's escalation_contact, as does a Tool
reaching its FailureThreshold (`Gates.escalate`) or a Step hitting a loop bound
(`Gates.bounded`, ADR 0008). An approver may decide through a signed email link
(cograil.approval_links, issue #24); `via` then names the channel on the AuditEvent. An
Approval expires after harness.yaml's approvals.timeout_hours.

Interim rules until their issues land: the approver is the Colleague's escalation_contact
(approver routing from decision tables still needs a directory lookup from tier to
principal), and escalating records the Run's status and a
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
from cograil.identity import normalise_principal_id, same_principal
from cograil.observability import log_event
from cograil.providers.base import Message, PlannedToolCall, StepComplete
from cograil.registry import CallContext
from cograil.run_versions import RunVersions
from cograil.store import RunStore

Clock = Callable[[], datetime]
GateAuditKind = Literal[
    "gate.paused", "gate.resumed", "gate.spent", "gate.refused", "loop.bounded", "run.escalated"
]
EscalationReason = Literal[
    "approval_declined",
    "approval_expired",
    "approver_is_principal",
    "failure_threshold",
    "loop_budget_exceeded",
]
RefusalReason = Literal["not_approver", "run_version_changed"]


def needs_approval(tool: Tool) -> bool:
    return tool.scope == "write" and tool.confirm_before_write


def new_approval_token() -> str:
    """128 random bits as 32 lowercase hex characters: a token never starts with `-`, so
    `cograil approve <token>` cannot mistake it for an option."""
    return secrets.token_bytes(16).hex()


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
        """Ask for an Approval of this call and park the Run awaiting_approval.

        When the approver would be the Run's own principal, who may never decide it, no
        Approval is created and the Run escalates now. With no Approval row to hold them, the
        attempted call's args go in the `run.escalated` detail (#107), and the Step's progress
        is saved in `Run.context["paused"]` as for a declined or expired Approval.
        """
        approver = self._colleague.escalation_contact
        if same_principal(approver, run.principal_id):
            paused = progress.model_dump(mode="json")
            run = run.model_copy(update={"context": {**run.context, "paused": paused}})
            detail = {"step": progress.step, "tool": tool.name, "args": dict(args),
                      "approver": approver}  # fmt: skip
            return await self.escalate(run, "approver_is_principal", detail)
        approval = Approval(
            token=new_approval_token(),
            run_id=run.id,
            step=progress.step,
            tool=tool.name,
            args=dict(args),
            approver=approver,
            expires_at=self._clock() + self._timeout,
        )
        paused = progress.model_copy(update={"token": approval.token}).model_dump(mode="json")
        context = {**run.context, "paused": paused}
        run = run.model_copy(update={"status": RunStatus.awaiting_approval, "context": context,
                                     "updated_at": self._clock()})  # fmt: skip
        # One transaction fenced by the claim: an execution whose claim was taken over gets
        # RunClaimLost and leaves no pending Approval behind (#143). The gate.paused
        # AuditEvent is written in it too, so a crash cannot leave a pause without it (#173).
        detail = {**_about(approval), "approver": approval.approver,
                  "expires_at": approval.expires_at}  # fmt: skip
        event = self._event(run, "gate.paused", detail)
        await self._store.create_approval(approval, run=run, events=[event])
        log_event("gate.paused", step=approval.step, tool=approval.tool)
        return run

    async def spend(self, run: Run, approval: Approval) -> None:
        """Let the one call an Approval authorises through; losing a race raises GateRequired.

        An execution whose claim on the Run was taken over spends nothing: RunClaimLost.
        The gate.spent AuditEvent is written in the spend's transaction (#173)."""
        event = self._event(run, "gate.spent", _about(approval))
        try:
            await self._store.spend_approval(approval.token, self._clock(), run=run, events=[event])
        except ApprovalNotSpendable as exc:
            raise GateRequired(f"{approval.tool}: its Approval is already spent") from exc

    async def resume(
        self,
        token: str,
        decision: Literal["approved", "declined"],
        decider: str,
        *,
        versions: RunVersions,
        via: str | None = None,
    ) -> Run:
        """Decide the Approval the Run is paused on: running again, or escalated.

        Raises ApprovalNotAllowed, leaving the Run and the Approval as they were, unless
        `decider` is the Approval's approver and not the Run's own principal. An empty
        `decider` is refused before anything is read or audited: an AuditEvent needs a
        principal (product rule 6). An approval of a Run started under other `versions` is
        refused the same way with ToolPackChanged or HarnessChanged (issue #97); a decline is not.
        `via` names the channel the decision came through (`email_link`), written on the
        `gate.resumed` or `run.escalated` AuditEvent beside the decider (issue #24).
        """
        decider = normalise_principal_id(decider)  # one spelling in the audit trail
        if not decider:
            raise ApprovalNotAllowed("an Approval is decided by a named principal, not by nobody")
        approval, run = await self._paused_on(token)
        if not same_principal(decider, approval.approver) or same_principal(
            decider, run.principal_id
        ):
            await self._refuse(run, approval, decider, "not_approver")
            raise ApprovalNotAllowed(f"{decider} may not decide the Approval for {approval.tool}")
        if self._overdue(approval):
            return await self._decide(run, approval, "expired", decider, via)
        refusal = versions.refusal(run) if decision == "approved" else None
        if refusal is not None:  # an approval would run Tool code under changed versions
            await self._refuse(run, approval, decider, "run_version_changed", refusal.changes)
            raise refusal
        return await self._decide(run, approval, decision, decider, via)

    async def expire(self, token: str) -> Run:
        """Escalate the Run if its Approval is past expires_at; otherwise change nothing."""
        approval, run = await self._paused_on(token)
        if not self._overdue(approval):
            return run
        return await self._decide(run, approval, "expired", None)

    async def escalate(self, run: Run, reason: EscalationReason, detail: dict[str, Any]) -> Run:
        """Stop the Run and hand it to the Colleague's escalation_contact."""
        run = await self._save(run, status=RunStatus.escalated)
        await self._announce_escalation(run, reason, detail)
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

    async def _refuse(
        self,
        run: Run,
        approval: Approval,
        decider: str,
        reason: RefusalReason,
        changes: Mapping[str, Any] | None = None,
    ) -> None:
        """Audit a refused decision, with the decider as the acting principal (#102, #182)."""
        detail = {**_about(approval), "approver": approval.approver, "decided_by": decider,
                  "run_principal": run.principal_id, "reason": reason}  # fmt: skip
        if changes is not None:
            detail["changes"] = dict(changes)
        await self._audit(run, "gate.refused", detail, principal=decider)
        log_event("gate.refused", logging.WARNING, run_id=run.id, step=approval.step,
                  reason=reason)  # fmt: skip

    async def _decide(
        self,
        run: Run,
        approval: Approval,
        decision: Literal["approved", "declined", "expired"],
        decider: str | None,
        via: str | None = None,
    ) -> Run:
        """`decider` is None when the Approval timed out with nobody deciding it."""
        approved = decision == "approved"
        status = RunStatus.running if approved else RunStatus.escalated
        run = run.model_copy(update={"status": status, "updated_at": self._clock()})
        # decide_approval succeeds once per Approval, so only one racing resume gets past it,
        # and it saves the Run in the same transaction, so neither changes without the other.
        # Its AuditEvent is written in that transaction too (product rule 6).
        detail = {**_about(approval), "approver": approval.approver, "decided_by": decider}
        if via is not None:
            detail["via"] = via
        reason: EscalationReason = (
            "approval_declined" if decision == "declined" else "approval_expired"
        )
        event = (
            self._event(run, "gate.resumed", detail)
            if approved
            else self._escalation(run, reason, detail)
        )
        await self._store.decide_approval(
            approval.token, decision, self._clock(), run=run, events=[event]
        )
        if approved:
            log_event("gate.resumed", run_id=run.id, step=approval.step, tool=approval.tool)
        else:
            self._log_escalation(run, reason)
        return run

    async def _announce_escalation(
        self, run: Run, reason: EscalationReason, detail: dict[str, Any]
    ) -> None:
        await self._store.append_audit_event(self._escalation(run, reason, detail))
        self._log_escalation(run, reason)

    def _escalation(self, run: Run, reason: EscalationReason, detail: dict[str, Any]) -> AuditEvent:
        contact = self._colleague.escalation_contact
        return self._event(run, "run.escalated", {"reason": reason, "contact": contact, **detail})

    def _log_escalation(self, run: Run, reason: EscalationReason) -> None:
        contact = self._colleague.escalation_contact
        log_event("run.escalated", logging.WARNING, run_id=run.id, reason=reason, contact=contact)

    async def _save(self, run: Run, **changes: Any) -> Run:
        run = run.model_copy(update={**changes, "updated_at": self._clock()})
        await self._store.update_run(run)
        return run

    async def _audit(
        self, run: Run, kind: GateAuditKind, detail: dict[str, Any], principal: str | None = None
    ) -> None:
        """`principal` is who acted, when that is not the Run's own principal."""
        await self._store.append_audit_event(self._event(run, kind, detail, principal))

    def _event(
        self, run: Run, kind: GateAuditKind, detail: dict[str, Any], principal: str | None = None
    ) -> AuditEvent:
        acting = run.principal_id if principal is None else principal
        return AuditEvent(
            run_id=run.id, at=self._clock(), principal_id=acting, kind=kind, detail=detail
        )


def _about(approval: Approval) -> dict[str, Any]:
    """What a gate AuditEvent says about its Approval."""
    return {"step": approval.step, "tool": approval.tool, "token": approval.token}


def utc_now() -> datetime:
    return datetime.now(UTC)
