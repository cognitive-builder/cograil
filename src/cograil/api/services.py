"""Services: what the routers share, the workspace, the store and the way to run a Colleague.

Each request that runs something builds a Runner over a ProgressStore, so the AuditEvents and
finished Steps of the Run stream out as they are written. The decider of an Approval is always
the signed-in Principal (`decide`); nothing a client sends can name another.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cograil.api.schemas import PendingApproval, RunOutcome, RunSummary
from cograil.api.sse import Emit
from cograil.audience import check_audience
from cograil.domain import (
    AuditEvent,
    Colleague,
    Principal,
    Protocol,
    Run,
    Trigger,
    Workspace,
)
from cograil.errors import ToolNotFound, WorkspaceError
from cograil.identity import same_principal
from cograil.orchestrator import Routing, classify_intent
from cograil.progress import ProgressStore
from cograil.providers.base import Provider
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import RunStore

type ProviderFactory = Callable[[Protocol, Colleague], Provider]
type RegistryOpener = Callable[[Workspace, RunStore, Path], Awaitable[ToolRegistry]]

CHANNEL = "web"


class Services:
    def __init__(
        self,
        workspace: Workspace,
        path: Path,
        store: RunStore,
        *,
        classifier: Provider,
        provider_for: ProviderFactory,
        open_registry: RegistryOpener,
    ) -> None:
        self.workspace = workspace
        self.path = path
        self.store = store
        self.tasks: set[asyncio.Task[None]] = set()
        self._classifier = classifier
        self._provider_for = provider_for
        self._open_registry = open_registry

    def protocol(self, name: str) -> Protocol | None:
        return next((p for p in self.workspace.protocols if p.name == name), None)

    def pick(self, protocol_name: str, colleague_name: str) -> tuple[Protocol, Colleague]:
        """The Protocol and Colleague by name; a workspace that lacks either is an error."""
        protocol = self.protocol(protocol_name)
        colleague = next((c for c in self.workspace.colleagues if c.name == colleague_name), None)
        if protocol is None or colleague is None:
            raise WorkspaceError(f"{colleague_name}/{protocol_name} is not in this workspace")
        return protocol, colleague

    @asynccontextmanager
    async def runner(
        self, protocol: Protocol, colleague: Colleague, emit: Emit
    ) -> AsyncIterator[tuple[Runner, ProgressStore]]:
        """A Runner whose store reports progress to `emit`, over the Tools of the workspace.

        Refuses, before anything runs, a Protocol with a Step that names a Tool nothing
        implements.
        """
        store = ProgressStore(self.store, lambda line: emit("progress", {"line": line}))
        async with await self._open_registry(self.workspace, store, self.path) as registry:
            for step in protocol.steps:
                for name in step.tools:
                    try:
                        registry.get(name)
                    except ToolNotFound as exc:
                        raise ToolNotFound(
                            f"step {step.number}: tool {name} is not available"
                        ) from exc
            harness = self.workspace.harness
            provider = self._provider_for(protocol, colleague)
            yield Runner(provider, registry, store, colleague, harness=harness), store

    async def chat(self, principal: Principal, message: str, emit: Emit) -> None:
        """Route a message, then start and run the Run it asks for, emitting as it goes."""
        routing = await classify_intent(self.workspace, principal, message, self._classifier)
        emit("routed", routing.model_dump(include={"colleague", "protocol", "confidence"}))
        if not routing.matched or routing.protocol is None or routing.colleague is None:
            emit("refusal", {"text": routing.refusal or ""})
            return
        protocol, colleague = self.pick(routing.protocol, routing.colleague)
        check_audience(self.workspace, colleague, protocol, principal)
        async with self.runner(protocol, colleague, emit) as (runner, store):
            run = self._new_run(protocol, colleague, principal)
            await store.create_run(run)
            emit("run", {"run_id": run.id})
            await store.append_audit_event(_classified(run, routing))
            ended = await runner.run(run.id, protocol)
            emit("done", (await self.outcome(ended)).model_dump(mode="json"))

    async def decide(
        self, token: str, principal: Principal, decision: Literal["approved", "declined"]
    ) -> RunOutcome:
        """Decide the Approval as `principal`; the Runner refuses anyone but its approver."""
        approval = await self.store.get_approval(token)
        run = await self.store.get_run(approval.run_id)
        protocol, colleague = self.pick(run.protocol, run.colleague)
        if run.workspace != self.workspace.name or protocol.version != run.protocol_version:
            raise WorkspaceError(
                f"run {run.id} was started on {run.workspace} protocol "
                f"{run.protocol} version {run.protocol_version}, which this workspace no longer has"
            )
        async with self.runner(protocol, colleague, _ignore) as (runner, _):
            ended = await runner.resume(token, protocol, decider=principal.id, decision=decision)
        return await self.outcome(ended, approver=principal.id)

    async def outcome(self, run: Run, *, approver: str | None = None) -> RunOutcome:
        """Where a Run stands; `approver` limits `awaiting` to that approver's own gates.

        The Run's principal (its chat) is shown every pending gate as the pointer of a paused
        Run; a deciding approver is shown only their own, as an Approval's token is the
        approver's (schemas.py)."""
        pending = [a for a in await self.store.list_approvals(run.id) if a.decision == "pending"]
        if approver is not None:
            pending = [a for a in pending if same_principal(a.approver, approver)]
        return RunOutcome(
            run=RunSummary.of(run),
            awaiting=[
                PendingApproval(token=a.token, approver=a.approver, tool=a.tool, step=a.step)
                for a in pending
            ],
        )

    def _new_run(self, protocol: Protocol, colleague: Colleague, principal: Principal) -> Run:
        """A received Run; its workspace folder is recorded so `cograil approve` can resume it."""
        now = datetime.now(UTC)
        trigger = Trigger(kind="chat", channel=CHANNEL)
        return Run(
            id=uuid.uuid4().hex,
            workspace=self.workspace.name,
            colleague=colleague.name,
            protocol=protocol.name,
            protocol_version=protocol.version,
            principal=principal,
            principal_id=principal.id,
            trigger=trigger,
            trigger_kind=trigger.kind,
            created_at=now,
            updated_at=now,
            context={"workspace_path": str(self.path.resolve())},
        )


def _ignore(event: str, data: dict[str, object]) -> None:
    """The Emit of a request that does not stream."""


def _classified(run: Run, routing: Routing) -> AuditEvent:
    """The classification as an AuditEvent of the Run it started, with the principal (rule 6)."""
    detail = routing.model_dump(include={"colleague", "protocol", "confidence", "reason", "model"})
    return AuditEvent(
        run_id=run.id,
        at=datetime.now(UTC),
        principal_id=run.principal_id,
        kind="orchestrator.classified",
        detail=detail,
    )
