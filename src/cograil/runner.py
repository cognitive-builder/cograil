"""Runner: executes a Protocol as a LangGraph graph with one node per Step (ADR 0011).

Inside a Step the model plans through the injected Provider and is offered only the
Step's whitelisted Tools. Every call of a plan is checked before any of them runs: a Tool
the Step does not list raises ToolNotAllowed, and a gated write needs an approved Approval
(gates.py). Only then does the call go to `ToolRegistry.invoke`, which checks neither; this
module is its only caller.

A gated write without an Approval pauses the Run awaiting_approval with the Step's progress
saved, and nothing from that plan runs; `Runner.resume` with the Approval's token, decided
by its approver (never the Run's own principal), continues the Run exactly there, running
the saved plan. A declined or expired Approval escalates the
Run, and so does a Tool reaching its FailureThreshold from the Protocol's Error handling:
until then a failed call (ToolExecutionError) goes back to the model as data, while a failed
call of a Tool with no threshold fails the Run. A paused or escalated Run ends the graph.

A Step's output (its final text and its tool results) is added to `Run.context["steps"]`,
and `Run.cursor` moves to the Step's number only once the Step has completed; both are
saved together. Any error fails the Run closed: status failed, a `run.failed` AuditEvent
with the principal, the cursor left at the last completed Step, and the error re-raised.

A Step ends only on the structured step_complete signal or a bound (ADR 0008). A plan with
neither tool calls nor the signal gets a reminder and another turn; a plan with both runs
its calls first, and if one of them failed the model gets another turn instead. Before
every provider call the Step is checked against the Harness bounds
(harness.py); a breach raises LoopBudgetExceeded, which escalates the Run through
`Gates.bounded`. Each call's tokens count against the Step and its cost against the Run.
`Runner.run` stamps the harness version on the Run (ADR 0012).

`Runner.run` and `Runner.resume` claim the Run (claims.py, `RunStore.claim_run`): of two
executions only the last claim's saves land, and the other stops with RunClaimLost without
failing the Run.

What a Step sees is the ContextBuilder's (context.py, ADR 0007): the prior Steps it declares,
its whitelisted Tools' schemas, and Tool results inside a data block. The Window Ledger of
that is kept in `Run.context["ledger"]`, saved with the Run.
"""

from __future__ import annotations

import itertools
import typing
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from cograil.claims import RunClaims
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
from cograil.harness import call_cost, check_bounds, harness_version
from cograil.observability import log_event
from cograil.providers.base import Message, Plan, PlannedToolCall, Provider
from cograil.registry import CallContext, ToolRegistry
from cograil.store import RunStore

_ENDED = frozenset({RunStatus.escalated, RunStatus.failed, RunStatus.completed})
NOT_COMPLETE = "The step is not complete until you signal step_complete."


class RunState(TypedDict):
    run: Run


class StepNode(typing.Protocol):
    async def __call__(self, state: RunState) -> RunState: ...


Graph = CompiledStateGraph[RunState, None, RunState, RunState]


def node_name(step: Step) -> str:
    return f"step_{step.number}"


def compile_protocol(protocol: Protocol, node_for: Callable[[Step], StepNode]) -> Graph:
    """One node per Step, edges in step order; entry is the first Step after Run.cursor.

    A Step that leaves the Run anything but running (paused or escalated) ends the graph.
    """
    graph = StateGraph(RunState)
    names = [node_name(step) for step in protocol.steps]
    for step, name in zip(protocol.steps, names, strict=True):
        graph.add_node(name, node_for(step))
    for here, there in itertools.pairwise(names):
        graph.add_conditional_edges(here, _onward(there), [there, END])
    graph.add_edge(names[-1], END)

    def entry(state: RunState) -> str:
        cursor = state["run"].cursor
        pending = [node_name(step) for step in protocol.steps if step.number > cursor]
        return pending[0] if pending else END

    graph.add_conditional_edges(START, entry, [*names, END])
    return graph.compile()


def _onward(there: str) -> Callable[[RunState], str]:
    def route(state: RunState) -> str:
        return there if state["run"].status is RunStatus.running else END

    return route


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
        self._claims = RunClaims(store, clock)
        self._harness = harness or Harness()
        self._context = ContextBuilder(self._harness)
        timeout = timedelta(hours=self._harness.approvals.timeout_hours)
        self._gates = Gates(store, colleague, timeout=timeout, clock=clock)

    async def run(self, run_id: str, protocol: Protocol) -> Run:
        """Run from the Step after Run.cursor until the end, a gate or an escalation.

        A Run awaiting approval moves on only through `resume`: this raises GateRequired.
        An escalated, failed or completed Run never runs again: this raises RunEnded.
        Another execution claiming the Run first raises RunClaimLost.
        """
        run = await self._store.get_run(run_id)
        if run.status is RunStatus.awaiting_approval:
            raise GateRequired(f"run {run_id} is awaiting approval; resume it with its token")
        if run.status in _ENDED:
            raise RunEnded(f"run {run_id} is {run.status}; it does not run again")
        version = harness_version(self._harness)
        async with self._claims.failing_closed(run_id) as claim:
            run = await self._claims.claim(run, claim, harness_version=version)
            detail = {"protocol": protocol.name, "version": protocol.version,
                      "harness_version": version, "cursor": run.cursor}  # fmt: skip
            await self._claims.audit(run, "run.started", detail)
            return await self._execute(run, protocol)

    async def resume(
        self,
        token: str,
        protocol: Protocol,
        *,
        decider: str,
        decision: Literal["approved", "declined"] = "approved",
    ) -> Run:
        """Decide the Approval a Run is paused on, then go on exactly at the paused Step.

        `decider` is the principal deciding it, who must be the Approval's approver. Declined
        or expired, the Run escalates instead. Errors in deciding (ApprovalNotAllowed for a
        decider who is empty, not the approver or the Run's own principal, RunNotPaused,
        ApprovalAlreadyDecided for a resume that lost a race) leave the Run as it was.
        RunClaimLost means a `run` claimed the decided Run first and goes on with it.
        """
        run = await self._gates.resume(token, decision, decider)
        if run.status is not RunStatus.running:
            return run
        async with self._claims.failing_closed(run.id) as claim:
            run = await self._claims.claim(run, claim)
            return await self._execute(run, protocol)

    async def expire(self, token: str) -> Run:
        """Escalate the Run paused on this Approval if it timed out; for a scheduler."""
        return await self._gates.expire(token)

    async def _execute(self, run: Run, protocol: Protocol) -> Run:
        graph = compile_protocol(protocol, lambda step: self._node(step, protocol))
        final = await graph.ainvoke({"run": run}, {"recursion_limit": len(protocol.steps) + 1})
        run = final["run"]
        if run.status is not RunStatus.running:  # paused or escalated, already saved
            return run
        run = await self._claims.save(run, status=RunStatus.completed)
        await self._claims.audit(run, "run.completed", {"cursor": run.cursor})
        return run

    def _node(self, step: Step, protocol: Protocol) -> StepNode:
        async def node(state: RunState) -> RunState:
            return {"run": await self._run_step(step, state["run"], protocol)}

        return node

    async def _run_step(self, step: Step, run: Run, protocol: Protocol) -> Run:
        groups = tuple(run.principal.groups)
        ctx = CallContext(run.id, step.number, run.principal_id, groups)
        tools = self._context.tools_for(step, self._registry.get)
        progress = paused_progress(run, step.number)
        if progress is None:
            opening = self._context.opening(run, step, tools)
            progress = StepProgress(step=step.number, messages=opening.messages)
            context = add_to_ledger(run.context, step.number, opening.tokens, restart=True)
            run = run.model_copy(update={"context": context})
        context = {name: value for name, value in run.context.items() if name != "paused"}
        run = run.model_copy(update={"context": context})
        while True:
            if not progress.planned:
                try:
                    run, plan = await self._turn(run, step, progress, tools)
                except LoopBudgetExceeded as exc:
                    return await self._gates.bounded(run, step.number, exc)
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
            progress.calls.extend(done)
            run = self._remember(run, step, progress, done)
            if progress.completing is not None:
                if not any("error" in call for call in done):
                    return await self._complete(run, step, progress.completing.output, progress)
                progress.completing = None  # it was said before a call failed: another turn

    def _remember(
        self, run: Run, step: Step, progress: StepProgress, done: list[dict[str, Any]]
    ) -> Run:
        """Give the model the results of the calls (as data) and count them in the ledger."""
        if not done:
            return run
        by_name = {call["tool"]: self._registry.get(call["tool"]) for call in done}
        results = self._context.tool_results(done, by_name)
        progress.messages.extend(results.messages)
        context = add_to_ledger(run.context, step.number, results.tokens)
        return run.model_copy(update={"context": context})

    async def _turn(
        self, run: Run, step: Step, progress: StepProgress, tools: list[Tool]
    ) -> tuple[Run, Plan]:
        """One provider call inside the Step's bounds; raises LoopBudgetExceeded past them."""
        check_bounds(self._harness, step, turns=progress.turn, tokens=progress.tokens,
                     cost_usd=run.cost_usd)  # fmt: skip
        plan = await self._provider.plan(step, progress.messages, tools)
        progress.turn += 1
        usage = plan.usage
        progress.tokens += usage.input_tokens + usage.output_tokens
        cost = call_cost(self._harness, plan.model, usage.input_tokens, usage.output_tokens)
        run = run.model_copy(update={"cost_usd": run.cost_usd + cost})
        if plan.text:
            progress.messages.append(Message(role="assistant", content=plan.text))
        return run, plan

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
                run = await self._count_failure(run, ctx, call, exc, protocol)
                if run.status is not RunStatus.running:
                    return run, done
                done.append({"tool": call.tool, "args": call.args, "error": str(exc)})
                continue
            done.append({"tool": call.tool, "args": call.args, "result": result})
        return run, done

    async def _count_failure(
        self,
        run: Run,
        ctx: CallContext,
        call: PlannedToolCall,
        exc: ToolExecutionError,
        protocol: Protocol,
    ) -> Run:
        """Count a failed call against the Tool's FailureThreshold; without one, fail the Run."""
        threshold = next((t for t in protocol.failure_thresholds if t.tool == call.tool), None)
        if threshold is None:
            raise exc
        failures = {**run.context.get("failures", {})}
        failures[call.tool] = failures.get(call.tool, 0) + 1
        run = run.model_copy(update={"context": {**run.context, "failures": failures}})
        if failures[call.tool] < threshold.max_failures:
            return run
        detail = {"step": ctx.step, "tool": call.tool, "failures": failures[call.tool],
                  "rule": threshold.rule, "error": str(exc)}  # fmt: skip
        return await self._gates.escalate(run, "failure_threshold", detail)

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
