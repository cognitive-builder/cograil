"""Running eval cases: one Run per case on an in-memory RunStore (issue #30, ADR 0013).

Three levels, from free to dear:

- `fake`: every case, on the FakeProvider replaying each case's script. Costs $0: the fake
  model is priced at zero, so the report still shows each case's tokens. The unit suite runs
  this level on every pull request.
- `smoke`: the cases marked `smoke`, on the live provider with every tier mapped to the small
  tier's model. For a quick check on demand.
- `full`: every case a real model can play, on the live provider's own tiers. Only for
  releases, and only through the provider's batch path at its lower price; until that path
  exists (issue #55) this level refuses to run rather than pay the interactive price.

A case's Run is the one `cograil run` would start: the workspace's Tools (its knowledge synced
into an in-memory store), its harness (bounds, tiers and prices), the audience check, and each
gate decided by its approver as the case says. Every model call is metered, so each case
records its tokens beside the Run's own cost.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cograil.audience import audience_denial, resolve_principal
from cograil.domain import (
    Colleague,
    Effort,
    Harness,
    Protocol,
    RunStatus,
    Step,
    TierModels,
    Tiers,
    Tool,
    Workspace,
)
from cograil.errors import CograilError, EvalError
from cograil.evals.cases import DENIED, EvalCase, EvalModel, mismatches
from cograil.knowledge.store import InMemoryKnowledgeStore, KnowledgeStore
from cograil.knowledge.sync import sync_workspace
from cograil.knowledge.tool import add_knowledge
from cograil.providers.base import Message, Plan, Provider, Usage
from cograil.providers.fake import fake_priced
from cograil.redaction import redactor
from cograil.registry import ToolRegistry, build_registry
from cograil.run_input import received_run
from cograil.runner import Runner
from cograil.store import InMemoryRunStore, RunStore

BATCH_ISSUE = 55


class Level(StrEnum):
    fake = "fake"
    smoke = "smoke"
    full = "full"


class CaseResult(EvalModel):
    """How one case went, with what its Run spent."""

    id: str
    protocol: str
    status: str
    error: str | None = None
    mismatches: list[str]
    usage: Usage
    cost_usd: float

    @property
    def passed(self) -> bool:
        return not self.mismatches

    @property
    def resolved(self) -> bool:
        """Completed without escalation: what cost per resolved run counts (ADR 0013)."""
        return self.status == RunStatus.completed


def choose_level(asked: Level | None, live: bool) -> Level:
    """The level to run: fake unless COGRAIL_LIVE=1 (`live`); live, smoke unless asked.

    Raises EvalError for the full level until the batch path exists (the full live suite runs
    only through it), and for a live level without `live`.
    """
    if asked is Level.full:
        raise EvalError(
            "the full live suite runs only through the provider's batch path, which is not "
            f"built yet (issue #{BATCH_ISSUE}); use --level smoke"
        )
    if not live:
        if asked in (None, Level.fake):
            return Level.fake
        raise EvalError(f"--level {asked} calls a real model; set COGRAIL_LIVE=1 to allow it")
    return asked or Level.smoke


def chosen(cases: Sequence[EvalCase], level: Level) -> list[EvalCase]:
    """The cases a level runs: all on fake, the smoke subset on smoke, and on full every case
    a real model can play."""
    if level is Level.fake:
        return list(cases)
    return [c for c in cases if not c.scripted_only and (c.smoke or level is Level.full)]


def level_harness(harness: Harness, level: Level) -> Harness:
    """The harness a level runs under: the fake model priced at $0 on fake, every tier on the
    small tier's model on smoke, and the workspace's own on full."""
    if level is Level.fake:
        return fake_priced(harness)
    if level is Level.full:
        return harness
    small = harness.models.small
    if harness.provider == "anthropic":
        return harness.model_copy(
            update={"tiers": Tiers(small=small, standard=small, strong=small)}
        )
    models = TierModels(small=small, standard=small, strong=small)
    return harness.model_copy(update={"providers": {**harness.providers, harness.provider: models}})


class Metered:
    """A Provider that adds up the Usage of every call it passes on."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider
        self.usage = Usage()

    async def plan(
        self,
        step: Step,
        context: Sequence[Message],
        tools: Sequence[Tool],
        *,
        model: str | None = None,
        effort: Effort | None = None,
        prefix: str = "",
    ) -> Plan:
        plan = await self._provider.plan(
            step, context, tools, model=model, effort=effort, prefix=prefix
        )
        spent, more = self.usage, plan.usage
        self.usage = Usage(
            input_tokens=spent.input_tokens + more.input_tokens,
            output_tokens=spent.output_tokens + more.output_tokens,
            cache_read_tokens=spent.cache_read_tokens + more.cache_read_tokens,
            cache_write_tokens=spent.cache_write_tokens + more.cache_write_tokens,
        )
        return plan


@dataclass(frozen=True)
class Suite:
    """What every case of one eval run shares."""

    workspace: Workspace
    root: Path
    harness: Harness
    provider_for: Callable[[EvalCase], Provider]
    live: bool
    knowledge: KnowledgeStore | None = None

    def pick(self, name: str) -> tuple[Protocol, Colleague]:
        protocol = next((p for p in self.workspace.protocols if p.name == name), None)
        colleague = next((c for c in self.workspace.colleagues if name in c.protocols), None)
        if protocol is None or colleague is None:
            raise EvalError(f"no colleague runs a protocol {name!r} in {self.workspace.name}")
        return protocol, colleague


async def run_suite(
    workspace: Workspace,
    root: Path,
    cases: Sequence[EvalCase],
    level: Level,
    provider_for: Callable[[EvalCase], Provider],
) -> list[CaseResult]:
    """Run the cases `level` chooses, one after another, each on a fresh RunStore."""
    knowledge: KnowledgeStore | None = None
    if any(tool.kind == "knowledge" for tool in workspace.tools):
        knowledge = InMemoryKnowledgeStore()
        await sync_workspace(root, workspace, knowledge)
    harness = level_harness(workspace.harness, level)
    suite = Suite(workspace, root, harness, provider_for, level is not Level.fake, knowledge)
    return [await run_case(suite, case) for case in chosen(cases, level)]


async def run_case(suite: Suite, case: EvalCase) -> CaseResult:
    """Run one case to its end or its first undecided gate, and compare the outcome."""
    protocol, colleague = suite.pick(case.protocol)
    principal = resolve_principal(suite.workspace, case.principal)
    if audience_denial(suite.workspace, colleague, protocol, principal) is not None:
        found = mismatches(case.expect, DENIED, None, [], [])
        return CaseResult(id=case.id, protocol=protocol.name, status=DENIED, mismatches=found,
                          usage=Usage(), cost_usd=0.0)  # fmt: skip
    store = InMemoryRunStore()
    provider = Metered(suite.provider_for(case))
    started = received_run(suite.workspace.name, suite.root, protocol, colleague, principal,
                           channel="eval", message=case.prompt)  # fmt: skip
    await store.create_run(started)
    async with await _registry(suite, store) as registry:
        runner = Runner(provider, registry, store, colleague, harness=suite.harness)
        error = await _drive(runner, store, started.id, protocol, case)
    run = await store.get_run(started.id)
    calls, gates = await store.list_tool_calls(run.id), await store.list_approvals(run.id)
    return CaseResult(id=case.id, protocol=protocol.name, status=run.status, error=error,
                      mismatches=mismatches(case.expect, run.status, error, calls, gates),
                      usage=provider.usage, cost_usd=run.cost_usd)  # fmt: skip


async def _registry(suite: Suite, store: RunStore) -> ToolRegistry:
    """The workspace's Tools, built fresh per case so the example tools' state starts anew."""
    registry = await build_registry(
        suite.workspace, store, suite.root, redactor=redactor(suite.workspace, suite.live)
    )
    if suite.knowledge is not None:
        add_knowledge(registry, suite.workspace, suite.knowledge)
    return registry


async def _drive(
    runner: Runner, store: RunStore, run_id: str, protocol: Protocol, case: EvalCase
) -> str | None:
    """Run the case, deciding each gate it meets in turn; the typed error it failed with."""
    try:
        run = await runner.run(run_id, protocol)
        for decision in case.decisions:
            pending = [a for a in await store.list_approvals(run_id) if a.decision == "pending"]
            if run.status is not RunStatus.awaiting_approval or not pending:
                break
            gate = pending[0]
            run = await runner.resume(
                gate.token, protocol, decider=gate.approver, decision=decision, via="eval"
            )
    except CograilError as exc:  # the case ends; the Run's status is what the store holds
        return type(exc).__name__
    return None
