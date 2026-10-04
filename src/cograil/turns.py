"""One model turn of a Step: the bounds check, the tier's model and escalation (ADR 0008, 0010).

A turn asks the Provider on the Step's tier, naming that tier's model and the Step's effort.
A small-tier `step_complete` whose confidence is below the harness's `min_confidence` is
discarded, with every tool call in the same plan, before anything in it is checked or run. The
turn is asked again one tier up, and a `tier.escalated` AuditEvent with the Run's principal
records it. Both calls count against the Step's bounds and the Run's cost.
"""

from __future__ import annotations

from cograil.claims import RunClaims
from cograil.context import add_cache_usage
from cograil.domain import Harness, Run, Step, Tier, Tool
from cograil.gates import StepProgress
from cograil.harness import check_bounds, escalation_tier, step_effort, tier_model
from cograil.observability import model_span
from cograil.providers.base import Message, Plan, Provider


class Turns:
    def __init__(self, provider: Provider, harness: Harness, claims: RunClaims) -> None:
        self._provider = provider
        self._harness = harness
        self._claims = claims

    async def take(
        self,
        run: Run,
        step: Step,
        tier: Tier,
        progress: StepProgress,
        tools: list[Tool],
        prefix: str = "",
    ) -> tuple[Run, Plan]:
        """One turn inside the Step's bounds; raises LoopBudgetExceeded past them.

        `prefix` is the Run's stable prompt prefix, sent first on every call (ADR 0013).
        The bounds see all the Run has spent, redactions included (issue #221).
        """
        run = self._claims.settled(run)
        self._check(run, step, progress)
        run, plan = await self._ask(run, step, tier, progress, tools, prefix)
        confidence = plan.step_complete.confidence if plan.step_complete else None
        higher = escalation_tier(self._harness, tier, confidence)
        if higher is not None:
            detail = {"step": step.number, "from_tier": tier, "to_tier": higher,
                      "confidence": confidence, "reason": "low confidence"}  # fmt: skip
            await self._claims.audit(run, "tier.escalated", detail)
            self._check(run, step, progress)
            run, plan = await self._ask(run, step, higher, progress, tools, prefix)
        progress.turn += 1
        if plan.text:
            progress.messages.append(Message(role="assistant", content=plan.text))
        return run, plan

    def _check(self, run: Run, step: Step, progress: StepProgress) -> None:
        check_bounds(self._harness, step, turns=progress.turn, tokens=progress.tokens,
                     cost_usd=run.cost_usd)  # fmt: skip

    async def _ask(
        self,
        run: Run,
        step: Step,
        tier: Tier,
        progress: StepProgress,
        tools: list[Tool],
        prefix: str,
    ) -> tuple[Run, Plan]:
        """One provider call on `tier`'s model; its tokens count against the Step, cost the Run.

        The call's cache reads and writes go to the Step's row of the Window Ledger.
        """
        model, effort = tier_model(self._harness, tier), step_effort(self._harness, step)
        with model_span(model):
            plan = await self._provider.plan(
                step, progress.messages, tools, model=model, effort=effort, prefix=prefix
            )
            usage = plan.usage
            cached = usage.cache_read_tokens + usage.cache_write_tokens
            progress.tokens += usage.input_tokens + usage.output_tokens + cached
            run = self._claims.charge(run, plan.model, usage)
        context = add_cache_usage(
            run.context, step.number, usage.cache_read_tokens, usage.cache_write_tokens
        )
        return run.model_copy(update={"context": context}), plan
