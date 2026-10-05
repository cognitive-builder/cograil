"""Runner: executes a Protocol as a LangGraph graph with one node per Step (ADR 0011).

The graph is compiled in graph.py (`compile_protocol`); this module supplies its Step nodes.

Inside a Step the model plans through the injected Provider and is offered only the Step's
whitelisted Tools. Every call of a plan is checked before any of them runs: a Tool the Step does not
list raises ToolNotAllowed, and a gated write needs an approved Approval (gates.py). Only then does
the call go to `ToolRegistry.invoke`, which checks neither; this module is its only caller.

A gated write without an Approval pauses the Run awaiting_approval with the Step's progress
saved, and nothing from that plan runs; `Runner.resume` with the Approval's token, decided
by its approver (never the Run's own principal), continues the Run exactly there, running
the saved plan. A declined or expired Approval escalates the Run, and so does a Tool reaching
its FailureThreshold from the Protocol's Error handling: until then a failed call
(ToolExecutionError) goes back to the model as data, while a failed call of a Tool with no
threshold fails the Run. A paused or escalated Run ends the graph.

A Step's output (its final text and its tool results) is added to `Run.context["steps"]`,
and `Run.cursor` moves to the Step's number only once the Step has completed; both are
saved together. Any error fails the Run closed: status failed, a `run.failed` AuditEvent
with the principal, the cursor left at the last completed Step, and the error re-raised.

A Step ends only on the structured step_complete signal or a bound (ADR 0008). A plan with
neither tool calls nor the signal gets a reminder and another turn; a plan with both runs
its calls first, and if one of them failed the model gets another turn instead. Before every
provider call the Step is checked against the Harness bounds (harness.py); a breach raises
LoopBudgetExceeded, which escalates the Run through `Gates.bounded`. Each call's tokens count
against the Step and its cost against the Run; the Run's spend is kept by its claim
(claims.py), so a Run that escalates or fails mid-Step still shows every call (issue #221).
`Runner.run` stamps the harness version on the Run (ADR 0012), and the registry's tool pack
version; an approval of a Run whose harness or tool pack has changed since is refused
(run_versions.py, issue #97), so an approved write runs under what the Run started with.

`Runner.run` and `Runner.resume` claim the Run (claims.py, `RunStore.claim_run`): of two
executions only the last claim's saves land; the other stops with RunClaimLost, not failed.

What a Step sees is the ContextBuilder's (context.py, ADR 0007): the prior Steps it declares,
its whitelisted Tools' schemas, and Tool results inside a data block. The Window Ledger of
that is kept in `Run.context["ledger"]`, saved with the Run. Tool results and failed calls are
handled in tool_results.py. Results pass the injection defence first (injection.py, issue #51):
instruction-like text is stripped and the small tier screens what is left. A result over the
harness's compression threshold then reaches the model as the small tier's summary
(compression.py); the raw result stays in the recorded `tool_calls`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from cograil.budget import Budget
from cograil.claims import RunClaims
from cograil.compression import Compressor
from cograil.context import ContextBuilder, add_to_ledger
from cograil.domain import (
    Approval,
    Colleague,
    Harness,
    Protocol,
    Run,
    RunStatus,
    Step,
    Tool,
)
from cograil.errors import (
    GateRequired,
    LoopBudgetExceeded,
    RunEnded,
    ToolExecutionError,
    ToolNotAllowed,
)
from cograil.gates import (
    Clock,
    Gates,
    StepProgress,
    paused_progress,
    require_approval,
    utc_now,
)
from cograil.graph import RunState, StepNode, compile_protocol
from cograil.harness import harness_version, step_tier
from cograil.injection import Screen
from cograil.observability import log_event, run_span, step_span
from cograil.providers.base import Message, PlannedToolCall, Provider
from cograil.registry import UNVERSIONED, CallContext, ToolRegistry
from cograil.run_versions import RunVersions
from cograil.store import RunStore
from cograil.tool_results import ToolResults
from cograil.turns import Turns

_ENDED = frozenset({RunStatus.escalated, RunStatus.failed, RunStatus.completed})
NOT_COMPLETE = "The step is not complete until you signal step_complete."


class Runner:
    """Executes Runs of a Colleague's Protocols. The Provider is injected (ADR 0005).

    The Harness is the workspace's (`Workspace.harness`); without one, the defaults.
    """

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        store: RunStore,
        colleague: Colleague,
        *,
        harness: Harness | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._store = store
        self._colleague = colleague
        self._harness = harness or Harness()
        self._claims = RunClaims(store, clock, self._harness, registry.redactor)
        self._context = ContextBuilder(self._harness)
        timeout = timedelta(hours=self._harness.approvals.timeout_hours)
        self._gates = Gates(store, colleague, timeout=timeout, clock=clock)
        self._budget = Budget(store, self._harness.budget, colleague, clock)
        self._turns = Turns(provider, self._harness, self._claims)
        screen = Screen(provider, self._harness, self._claims)
        compressor = Compressor(provider, self._harness, self._claims)
        self._results = ToolResults(
            registry, self._context, self._gates, screen, compressor, self._claims
        )

    async def run(self, run_id: str, protocol: Protocol) -> Run:
        """Run from the Step after Run.cursor until the end, a gate or an escalation.

        A Run awaiting approval moves on only through `resume`: this raises GateRequired.
        An escalated, failed or completed Run never runs again: this raises RunEnded.
        A Run already stamped with another tool pack or harness raises ToolPackChanged or
        HarnessChanged.
        Another execution claiming the Run first raises RunClaimLost.
        """
        run = await self._store.get_run(run_id)
        if run.status is RunStatus.awaiting_approval:
            raise GateRequired(f"run {run_id} is awaiting approval; resume it with its token")
        if run.status in _ENDED:
            raise RunEnded(f"run {run_id} is {run.status}; it does not run again")
        versions = self._versions()
        if run.tool_pack_version != UNVERSIONED:  # running again, as after a crash
            versions.require(run)
        version, pack = versions.harness_version, versions.tool_pack_version
        starting = run.status is RunStatus.received
        async with self._claims.failing_closed(run_id) as claim:
            run = await self._claims.claim(
                run, claim, harness_version=version, tool_pack_version=pack
            )
            if starting and (capped := await self._budget.over_cap(run)):
                return await self._gates.escalate(run, "monthly_cap_reached", capped)
            detail = {"protocol": protocol.name, "version": protocol.version,
                      "harness_version": version, "tool_pack_version": pack,
                      "cursor": run.cursor}  # fmt: skip
            await self._claims.audit(run, "run.started", detail)
            return await self._execute(run, protocol)

    async def resume(
        self,
        token: str,
        protocol: Protocol,
        *,
        decider: str,
        decision: Literal["approved", "declined"] = "approved",
        via: str | None = None,
    ) -> Run:
        """Decide the Approval a Run is paused on, then go on exactly at the paused Step.

        `decider` is the principal deciding it, who must be the Approval's approver. Declined
        or expired, the Run escalates instead. Errors in deciding (ApprovalNotAllowed for a
        decider who is empty, not the approver or the Run's own principal, RunNotPaused,
        ApprovalAlreadyDecided for a resume that lost a race) leave the Run as it was.
        RunClaimLost means a `run` claimed the decided Run first and goes on with it.
        An approval of a Run started with another tool pack or harness raises ToolPackChanged
        or HarnessChanged and leaves it as it was too; a decline still escalates it (#97).
        `via` names the channel the decision came through, for the AuditEvent (issue #24).
        """
        run = await self._gates.resume(token, decision, decider, versions=self._versions(), via=via)
        if run.status is not RunStatus.running:
            return run
        async with self._claims.failing_closed(run.id) as claim:
            run = await self._claims.claim(run, claim)
            return await self._execute(run, protocol)

    def _versions(self) -> RunVersions:
        """The harness and tool pack versions a Run runs under with this Runner."""
        return RunVersions(harness_version(self._harness), self._registry.tool_pack_version)

    async def expire(self, token: str, *, via: str | None = None) -> Run:
        """Escalate the Run paused on this Approval if it timed out; for a scheduler."""
        return await self._gates.expire(token, via=via)

    async def undeliverable(self, token: str) -> Run:
        """Escalate the Run paused on this Approval, which nothing could tell its approver of."""
        return await self._gates.undeliverable(token)

    async def _execute(self, run: Run, protocol: Protocol) -> Run:
        graph = compile_protocol(protocol, lambda step: self._node(step, protocol))
        with run_span(run, protocol.name):
            final = await graph.ainvoke({"run": run}, {"recursion_limit": len(protocol.steps) + 1})
        run = final["run"]
        if run.status is RunStatus.running:  # else paused or escalated, already saved
            run = await self._claims.save(run, status=RunStatus.completed)
            await self._claims.audit(run, "run.completed", {"cursor": run.cursor})
        await self._budget.alert_if_crossed(run)
        return run

    def _node(self, step: Step, protocol: Protocol) -> StepNode:
        async def node(state: RunState) -> RunState:
            with step_span(state["run"], step):
                return {"run": await self._run_step(step, state["run"], protocol)}

        return node

    async def _run_step(self, step: Step, run: Run, protocol: Protocol) -> Run:
        groups = tuple(run.principal.groups)
        ctx = CallContext(run.id, step.number, run.principal_id, groups,
                          self._claims.charge_to(run))  # fmt: skip
        tools = self._context.tools_for(step, self._registry.get)
        progress = paused_progress(run, step.number)
        if progress is None:
            opening = self._context.opening(run, step, tools)
            progress = StepProgress(step=step.number, messages=opening.messages)
            context = add_to_ledger(run.context, step.number, opening.tokens, restart=True)
            run = run.model_copy(update={"context": context})
        tier = step_tier(self._colleague, protocol, step)
        prefix = self._context.prefix(self._colleague, protocol)
        context = {name: value for name, value in run.context.items() if name != "paused"}
        run = run.model_copy(update={"context": context})
        while True:
            if not progress.planned:
                try:
                    run, plan = await self._turns.take(run, step, tier, progress, tools, prefix)
                except LoopBudgetExceeded as exc:
                    return await self._bounded(run, step, exc)
                if not plan.tool_calls:
                    if plan.step_complete is not None:
                        return await self._complete(run, step, plan.step_complete.output, progress)
                    progress.messages.append(Message(role="user", content=NOT_COMPLETE))
                    continue
                progress.planned, progress.completing = list(plan.tool_calls), plan.step_complete
            run, done = await self._act(run, step, ctx, progress, protocol)
            if run.status is not RunStatus.running:
                return run
            progress.planned = []
            try:
                run = await self._results.absorb(run, step, progress, done)
            except LoopBudgetExceeded as exc:
                return await self._bounded(run, step, exc)
            if progress.completing is not None:
                if not any("error" in call for call in done):
                    return await self._complete(run, step, progress.completing.output, progress)
                progress.completing = None  # it was said before a call failed: another turn

    async def _act(
        self, run: Run, step: Step, ctx: CallContext, progress: StepProgress, protocol: Protocol
    ) -> tuple[Run, list[dict[str, Any]]]:
        """Check every call of the plan, then run them; nothing runs if any check fails.

        A gated call without an Approval pauses the Run instead of raising.
        """
        tools = [self._allowed(step, call) for call in progress.planned]
        claimed: list[Approval] = []
        for tool, call in zip(tools, progress.planned, strict=True):
            try:
                approval = await require_approval(
                    self._store, ctx, tool, call.args, [a.token for a in claimed]
                )
            except GateRequired:
                return await self._gates.pause(run, tool, call.args, progress), []
            if approval is not None:
                claimed.append(approval)
        for approval in claimed:  # spend the Approvals before the writes they authorise
            await self._gates.spend(run, approval)
        return await self._invoke(run, ctx, progress.planned, protocol)

    async def _invoke(
        self, run: Run, ctx: CallContext, planned: list[PlannedToolCall], protocol: Protocol
    ) -> tuple[Run, list[dict[str, Any]]]:
        done: list[dict[str, Any]] = []
        for call in planned:
            try:
                result = await self._registry.invoke(call.tool, call.args, ctx)
            except ToolExecutionError as exc:
                run = await self._results.count_failure(run, ctx, call, exc, protocol)
                if run.status is not RunStatus.running:
                    return run, done
                done.append({"tool": call.tool, "args": call.args, "error": str(exc)})
                continue
            done.append({"tool": call.tool, "args": call.args, "result": result})
        return run, done

    async def _bounded(self, run: Run, step: Step, exc: LoopBudgetExceeded) -> Run:
        """Escalate on a breached bound with every call charged, those of the turn that raised."""
        return await self._gates.bounded(self._claims.settled(run), step.number, exc)

    def _allowed(self, step: Step, call: PlannedToolCall) -> Tool:
        if call.tool not in step.tools:
            raise ToolNotAllowed(f"step {step.number} does not list tool {call.tool!r}")
        return self._registry.get(call.tool)

    async def _complete(self, run: Run, step: Step, output: str, progress: StepProgress) -> Run:
        record = {"name": step.name, "output": output, "tool_calls": progress.calls}
        steps = {**run.context.get("steps", {}), str(step.number): record}
        context = {**run.context, "steps": steps}
        run = await self._claims.save(run, cursor=step.number, context=context)
        log_event("step.completed", step=step.number, tool_calls=len(progress.calls),
                  turns=progress.turn, tokens=progress.tokens)  # fmt: skip
        return run
