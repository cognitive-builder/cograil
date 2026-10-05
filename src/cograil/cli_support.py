"""Helpers shared by the `run`, `approve` and `decide` commands in `cograil.cli`."""

from __future__ import annotations

import os
from collections.abc import Awaitable
from pathlib import Path
from typing import Annotated

import typer

from cograil.cliexit import EXIT_BY_STATUS, EXIT_FAILED, fail
from cograil.domain import Colleague, Harness, Protocol, Run, RunStatus, Workspace
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotAllowed,
    CograilError,
    RunClaimLost,
    RunNotPaused,
    RunVersionChanged,
    ToolNotFound,
    WorkspaceError,
)
from cograil.knowledge.store import PostgresKnowledgeStore
from cograil.knowledge.tool import add_knowledge
from cograil.providers import FakeProvider, Provider, make_provider, provider_ready
from cograil.providers.fake import fake_priced, load_script
from cograil.redaction import redactor
from cograil.registry import ToolRegistry, build_registry
from cograil.store import RunStore
from cograil.workspace import load_workspace

AsOption = Annotated[
    str, typer.Option("--as", help="The principal acting (for approve, the deciding approver).")
]
FakeScript = Annotated[
    Path | None,
    typer.Option(
        "--fake-script",
        help="Use the FakeProvider with the plans in this YAML file instead of Anthropic. "
        "Ignored with --decline.",
    ),
]


def principal_id(value: str) -> str:
    if not value.strip():
        fail("--as must name a principal")
    return value.strip()


def load_or_fail(path: Path) -> Workspace:
    try:
        return load_workspace(path)
    except WorkspaceError as exc:
        fail(f"invalid: {exc}")


def pick_protocol(workspace: Workspace, name: str) -> tuple[Protocol, Colleague]:
    protocol = next((p for p in workspace.protocols if p.name == name), None)
    if protocol is None:
        fail(f"no protocol {name!r} in {workspace.name}")
    colleague = next((c for c in workspace.colleagues if protocol.name in c.protocols), None)
    if colleague is None:
        fail(f"no colleague runs protocol {protocol.name!r}")
    return protocol, colleague


def run_harness(workspace: Workspace, script: Path | None) -> Harness:
    """The harness the Runner gets: the Workspace's, with the fake model free for --fake-script."""
    return workspace.harness if script is None else fake_priced(workspace.harness)


def make_run_provider(workspace: Workspace, script: Path | None) -> Provider:
    if script is not None:
        try:
            return FakeProvider(load_script(script))
        except CograilError as exc:
            fail(str(exc))
    harness = workspace.harness
    if not provider_ready(harness):
        fail("ANTHROPIC_API_KEY is not set (or use --fake-script)")
    try:
        return make_provider(harness, harness.models.standard)
    except CograilError as exc:
        fail(str(exc))


async def build_tools(
    workspace: Workspace, store: RunStore, root: Path, live: bool
) -> ToolRegistry:
    try:
        registry = await build_registry(workspace, store, root, redactor=redactor(workspace, live))
    except CograilError as exc:
        fail(f"cannot build the tools: {exc}")
    url = os.environ.get("DATABASE_URL")
    if url and any(tool.kind == "knowledge" for tool in workspace.tools):
        knowledge = PostgresKnowledgeStore.from_url(url)  # the Chunks `knowledge sync` stored
        registry.on_close(knowledge.dispose)
        add_knowledge(registry, workspace, knowledge)
    return registry


def check_tools(workspace: Workspace, protocol: Protocol, registry: ToolRegistry) -> None:
    """Refuse to go on when a Step names a Tool nothing implements yet."""
    kinds = {tool.name: tool.kind for tool in workspace.tools}
    for step in protocol.steps:
        for name in step.tools:
            try:
                registry.get(name)
            except ToolNotFound:
                # build_tools leaves the knowledge kind out when DATABASE_URL is unset.
                hint = (
                    "; knowledge tools need DATABASE_URL"
                    if kinds[name] == "knowledge" and not os.environ.get("DATABASE_URL")
                    else ""
                )
                fail(f"step {step.number}: tool {name} (kind {kinds[name]}) is not available{hint}")


async def guarded_run(running: Awaitable[Run]) -> Run:
    """The Runner's result; a failed Run (closed by the Runner) exits with EXIT_FAILED."""
    try:
        return await running
    except CograilError as exc:
        fail(f"failed: {type(exc).__name__}: {exc}", EXIT_FAILED)


async def report_run(store: RunStore, colleague: Colleague, ended: Run) -> None:
    """Say how the Run stands, what to do next, and exit with the code of its status."""
    typer.echo(f"{ended.id}  {ended.status}  ${ended.cost_usd:.4f}")
    if ended.status is RunStatus.awaiting_approval:
        for approval in await store.list_approvals(ended.id):
            if approval.decision == "pending":
                typer.echo(
                    f"awaiting {approval.approver} to approve {approval.tool} (step "
                    f"{approval.step}): cograil approve {approval.token} --as {approval.approver}"
                )
    elif ended.status is RunStatus.escalated:
        typer.echo(f"escalated to {colleague.escalation_contact}")
    if ended.status in EXIT_BY_STATUS:
        raise typer.Exit(code=EXIT_BY_STATUS[ended.status])


async def guarded_decision(deciding: Awaitable[Run]) -> Run:
    """The Runner's result of a decision; refusals exit 1, a Run that failed exits 5."""
    try:
        return await deciding
    except (
        ApprovalNotAllowed,
        RunNotPaused,
        ApprovalAlreadyDecided,
        RunClaimLost,
        RunVersionChanged,
    ) as exc:
        fail(f"refused: {exc}")
    except CograilError as exc:
        fail(f"failed: {type(exc).__name__}: {exc}", EXIT_FAILED)
