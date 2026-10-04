"""What happens to a Step's tool calls once they ran, for the Runner (issue #145).

Results reach the model as data: screened for injected instructions first (injection.py,
issue #51), then long ones compressed by the small tier (compression.py), and counted in the
Window Ledger (context.py, ADR 0007). The Step's recorded `tool_calls` keep the raw results
with error text redacted (#78). A failed call counts against the Tool's FailureThreshold from
the Protocol's Error handling and escalates the Run at the limit; without a threshold it fails
the Run. Nothing here checks or invokes a Tool: that stays in the Runner. Each redaction's
model pass is charged to the Run (issue #221).
"""

from __future__ import annotations

from typing import Any

from cograil.claims import RunClaims
from cograil.compression import Compressor, recorded, shown
from cograil.context import SCREENED_KEY, ContextBuilder, add_to_ledger
from cograil.domain import Protocol, Run, Step
from cograil.errors import ToolExecutionError
from cograil.gates import Gates, StepProgress
from cograil.injection import Screen, kept
from cograil.providers.base import PlannedToolCall
from cograil.registry import CallContext, ToolRegistry


class ToolResults:
    """The Runner's handling of tool calls that already ran: results absorbed, failures counted."""

    def __init__(
        self,
        registry: ToolRegistry,
        context: ContextBuilder,
        gates: Gates,
        screen: Screen,
        compressor: Compressor,
        claims: RunClaims,
    ) -> None:
        self._claims = claims
        self._registry = registry
        self._context = context
        self._gates = gates
        self._screen = screen
        self._compressor = compressor

    async def absorb(
        self, run: Run, step: Step, progress: StepProgress, done: list[dict[str, Any]]
    ) -> Run:
        """Record the calls on the Step and give the model their results: screened for
        injected instructions first, then long ones compressed."""
        run, safe = await self._screen.screen(run, step, progress, done)
        run, summaries = await self._compressor.compress(run, step, progress, safe)
        progress.calls.extend(await self._recorded(run, recorded(kept(done, safe), summaries)))
        return self._claims.settled(self._remember(run, step, progress, shown(safe, summaries)))

    async def count_failure(
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
        charge = self._claims.charge_to(run)
        error = await self._registry.redactor.redact(str(exc), charge)  # the audit copy
        detail = {"step": ctx.step, "tool": call.tool, "failures": failures[call.tool],
                  "rule": threshold.rule, "error": error}  # fmt: skip
        return await self._gates.escalate(self._claims.settled(run), "failure_threshold", detail)

    async def _recorded(self, run: Run, done: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`done` as it is recorded in the Step's `tool_calls`: error text redacted (#78).

        The model's own messages are built from `done` as it is, so it still sees the raw error.
        """
        redact, charge = self._registry.redactor.redact, self._claims.charge_to(run)
        return [
            {**c, **{k: await redact(c[k], charge) for k in ("error", SCREENED_KEY) if k in c}}
            if "error" in c
            else c
            for c in done
        ]

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
