"""Services: what the routers share, the workspace, the store and the way to run a Colleague.

Each request that runs something builds a Runner over a ProgressStore, so the AuditEvents and
finished Steps of the Run stream out as they are written. The decider of an Approval is always
the signed-in Principal (`decide`); nothing a client sends can name another. The routing of a
chat message is charged to the Run it starts (issue #221).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cograil.api.approval_mail import ApprovalMail
from cograil.api.schemas import PendingApproval, RunOutcome, RunSummary
from cograil.api.sse import Emit
from cograil.audience import check_audience
from cograil.cost import charge_aside
from cograil.domain import (
    AuditEvent,
    Colleague,
    Principal,
    Protocol,
    Routing,
    Run,
    RunStatus,
    Workspace,
)
from cograil.errors import ToolNotFound, WorkspaceError
from cograil.identity import same_principal
from cograil.orchestrator import classify_intent
from cograil.progress import ProgressStore
from cograil.providers.base import Provider, Usage
from cograil.redaction import Redactor
from cograil.registry import ToolRegistry
from cograil.run_input import received_run
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
        redactor: Redactor | None = None,
        approval_mail: ApprovalMail | None = None,
    ) -> None:
        self.approval_mail = approval_mail
        self.workspace = workspace
        self.path = path
        self.store = store
        self.tasks: set[asyncio.Task[None]] = set()
        self._classifier = classifier
        self._provider_for = provider_for
        self._open_registry = open_registry
        self._redactor = redactor

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

    async def chat(
        self,
        principal: Principal,
        message: str,
        emit: Emit,
        *,
        channel: str = CHANNEL,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Route a message, then start and run the Run it asks for, emitting as it goes.

        `channel` names where the message came from; `context` is extra data for the Run's
        context (the Slack thread of a Run). The Run starts charged with the routing's model
        calls."""
        routing, routed = await self._route(principal, message, emit)
        if not routing.matched or routing.protocol is None or routing.colleague is None:
            emit("refusal", {"text": routing.refusal or ""})
            return
        protocol, colleague = self.pick(routing.protocol, routing.colleague)
        check_audience(self.workspace, colleague, protocol, principal)
        async with self.runner(protocol, colleague, emit) as (runner, store):
            run = received_run(
                self.workspace.name,
                self.path,
                protocol,
                colleague,
                principal,
                channel=channel,
                message=message,
                extra=context,
            )
            run = charge_aside(self.workspace.harness, run, routed)
            await store.create_run(run)
            emit("run", {"run_id": run.id})
            await store.append_audit_event(_classified(run, routing))
            ended = await runner.run(run.id, protocol)
            await self._email_approvers(ended)
            emit("done", (await self.outcome(ended)).model_dump(mode="json"))

    async def _route(
        self, principal: Principal, message: str, emit: Emit
    ) -> tuple[Routing, list[tuple[str, Usage]]]:
        """Classify the message and emit the routing, with the model calls the routing made."""
        routed: list[tuple[str, Usage]] = []
        routing = await classify_intent(
            self.workspace, principal, message, self._classifier, self._redactor,
            lambda model, usage: routed.append((model, usage)),
        )  # fmt: skip
        emit("routed", routing.model_dump(include={"colleague", "protocol", "confidence"}))
        return routing, routed

    async def decide(
        self,
        token: str,
        principal: Principal,
        decision: Literal["approved", "declined"],
        *,
        via: str | None = None,
    ) -> RunOutcome:
        """Decide the Approval as `principal`; the Runner refuses anyone but its approver.
        `via` names the channel the decision came through, for the AuditEvent."""
        return await self._decide(token, principal.id, decision, via=via)

    async def decide_by_link(
        self, token: str, decision: Literal["approved", "declined"]
    ) -> RunOutcome:
        """Decide the Approval as its approver, whom a verified email link stands for."""
        approval = await self.store.get_approval(token)
        return await self._decide(token, approval.approver, decision, via="email_link")

    async def expire(self, token: str) -> Run:
        """Escalate the Run paused on this Approval if it is past its expiry (an expired link)."""
        run = await self.store.get_run((await self.store.get_approval(token)).run_id)
        protocol, colleague = self._pick_for(run)
        async with self.runner(protocol, colleague, _ignore) as (runner, _):
            return await runner.expire(token)

    async def _decide(
        self,
        token: str,
        decider: str,
        decision: Literal["approved", "declined"],
        *,
        via: str | None,
    ) -> RunOutcome:
        run = await self.store.get_run((await self.store.get_approval(token)).run_id)
        protocol, colleague = self._pick_for(run)
        async with self.runner(protocol, colleague, _ignore) as (runner, _):
            ended = await runner.resume(
                token, protocol, decider=decider, decision=decision, via=via
            )
        await self._email_approvers(ended)
        return await self.outcome(ended, approver=decider)

    def _pick_for(self, run: Run) -> tuple[Protocol, Colleague]:
        """The Protocol and Colleague a stored Run started with, if the workspace still has them."""
        protocol, colleague = self.pick(run.protocol, run.colleague)
        if run.workspace != self.workspace.name or protocol.version != run.protocol_version:
            raise WorkspaceError(
                f"run {run.id} was started on {run.workspace} protocol "
                f"{run.protocol} version {run.protocol_version}, which this workspace no longer has"
            )
        return protocol, colleague

    async def _email_approvers(self, run: Run) -> None:
        if self.approval_mail is not None and run.status is RunStatus.awaiting_approval:
            await self.approval_mail.notify(self.store, run)

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
            output=_final_output(run),
            awaiting=[
                PendingApproval(token=a.token, approver=a.approver, tool=a.tool, step=a.step)
                for a in pending
            ],
        )


def _ignore(event: str, data: dict[str, object]) -> None:
    """The Emit of a request that does not stream."""


def _final_output(run: Run) -> str | None:
    """The answer a completed Run ended with: the newest output of its finished Steps. A Run
    that stopped for a gate, was declined or failed has none yet."""
    if run.status is not RunStatus.completed:
        return None
    steps: dict[str, dict[str, Any]] = run.context.get("steps", {})
    for number in range(run.cursor, 0, -1):
        output = steps.get(str(number), {}).get("output")
        if isinstance(output, str):
            return output
    return None


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
