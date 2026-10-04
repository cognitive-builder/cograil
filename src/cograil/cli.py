"""The cograil command line: validate, graph, run, approve, runs, decide and knowledge sync.

`run` and `approve` are a local and demo tool. `--as` is taken at face value: nothing here
authenticates the principal, and the safety comes from needing DATABASE_URL. A principal's
groups come from the workspace's principals.yaml. `approve` goes through `Runner.resume`, so
the approver check and the `gate.refused` AuditEvent hold; real authentication of the decider
belongs to the web and Slack channels (issue #108).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer

from cograil.audience import check_audience, resolve_principal
from cograil.cliexit import (
    EXIT_BY_STATUS,
    EXIT_CODES,
    EXIT_FAILED,
    await_command,
    fail,
)
from cograil.context import format_ledger, window_ledger
from cograil.decisions import parse_inputs, table_for
from cograil.domain import Colleague, Principal, Protocol, Run, RunStatus, Trigger, Workspace
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotAllowed,
    ApprovalNotFound,
    AudienceDenied,
    CograilError,
    DecisionError,
    RunClaimLost,
    RunNotFound,
    RunNotPaused,
    RunVersionChanged,
    ToolNotFound,
    WorkspaceError,
)
from cograil.graph_cli import graph
from cograil.knowledge.cli import knowledge_app
from cograil.knowledge.store import PostgresKnowledgeStore
from cograil.knowledge.tool import add_knowledge
from cograil.progress import ProgressStore
from cograil.providers import AnthropicProvider, FakeProvider, Provider
from cograil.providers.fake import load_script
from cograil.registry import ToolRegistry, build_registry
from cograil.runner import Runner
from cograil.store import PostgresRunStore, RunStore
from cograil.validate import validate_workspace
from cograil.workspace import load_workspace

app = typer.Typer(
    help="Cograil: run Markdown runbooks as an AI colleague.\n\n" + EXIT_CODES,
    no_args_is_help=True,
)
app.add_typer(knowledge_app, name="knowledge")
app.command()(graph)
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


@app.command()
def validate(
    path: Annotated[Path, typer.Argument(help="Workspace folder to validate.")],
) -> None:
    """Check a workspace and report every problem it has, not just the first.

    Besides loading every file, it checks that no Step whitelists two Tools whose Anthropic
    wire names collide (`a.b` and `a__b`).

    \b
    Exit codes: 0 valid; 1 one or more problems (all printed); 2 usage error.
    """
    problems = validate_workspace(path)
    if problems:
        for problem in problems:
            typer.echo(f"invalid: {problem}", err=True)
        fail(f"{len(problems)} problem(s) found in {path}")
    workspace = load_workspace(path)
    typer.echo(
        f"ok: {workspace.name} ({len(workspace.colleagues)} colleagues, "
        f"{len(workspace.protocols)} protocols, {len(workspace.tools)} tools)"
    )


def _principal_id(value: str) -> str:
    if not value.strip():
        fail("--as must name a principal")
    return value.strip()


def _load(path: Path) -> Workspace:
    try:
        return load_workspace(path)
    except WorkspaceError as exc:
        fail(f"invalid: {exc}")


def _pick(workspace: Workspace, name: str) -> tuple[Protocol, Colleague]:
    protocol = next((p for p in workspace.protocols if p.name == name), None)
    if protocol is None:
        fail(f"no protocol {name!r} in {workspace.name}")
    colleague = next((c for c in workspace.colleagues if protocol.name in c.protocols), None)
    if colleague is None:
        fail(f"no colleague runs protocol {protocol.name!r}")
    return protocol, colleague


def _provider(protocol: Protocol, colleague: Colleague, script: Path | None) -> Provider:
    if script is not None:
        try:
            return FakeProvider(load_script(script))
        except CograilError as exc:
            fail(str(exc))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        fail("ANTHROPIC_API_KEY is not set (or use --fake-script)")
    return AnthropicProvider.for_protocol(protocol, colleague)


async def _registry(workspace: Workspace, store: RunStore, root: Path) -> ToolRegistry:
    try:
        registry = await build_registry(workspace, store, root)
    except CograilError as exc:
        fail(f"cannot build the tools: {exc}")
    url = os.environ.get("DATABASE_URL")
    if url and any(tool.kind == "knowledge" for tool in workspace.tools):
        knowledge = PostgresKnowledgeStore.from_url(url)  # the Chunks `knowledge sync` stored
        registry.on_close(knowledge.dispose)
        add_knowledge(registry, workspace, knowledge)
    return registry


def _check_tools(workspace: Workspace, protocol: Protocol, registry: ToolRegistry) -> None:
    """Refuse to go on when a Step names a Tool nothing implements yet."""
    kinds = {tool.name: tool.kind for tool in workspace.tools}
    for step in protocol.steps:
        for name in step.tools:
            try:
                registry.get(name)
            except ToolNotFound:
                # _registry leaves the knowledge kind out when DATABASE_URL is unset.
                hint = (
                    "; knowledge tools need DATABASE_URL"
                    if kinds[name] == "knowledge" and not os.environ.get("DATABASE_URL")
                    else ""
                )
                fail(f"step {step.number}: tool {name} (kind {kinds[name]}) is not available{hint}")


def _new_run(
    workspace: Workspace, path: Path, protocol: Protocol, colleague: Colleague, principal: Principal
) -> Run:
    """A received Run. Trigger.kind has no "cli": a CLI Run is a chat on the channel "cli"."""
    now = datetime.now(UTC)
    trigger = Trigger(kind="chat", channel="cli")
    return Run(
        id=uuid.uuid4().hex,
        workspace=workspace.name,
        colleague=colleague.name,
        protocol=protocol.name,
        protocol_version=protocol.version,
        principal=principal,
        principal_id=principal.id,
        trigger=trigger,
        trigger_kind=trigger.kind,
        created_at=now,
        updated_at=now,
        context={"workspace_path": str(path.resolve())},
    )


@app.command()
def run(
    path: Annotated[Path, typer.Argument(help="Workspace folder.")],
    protocol: Annotated[str, typer.Option(help="Name of the Protocol to run.")],
    as_: AsOption,
    fake_script: FakeScript = None,
) -> None:
    """Start a Run of a Protocol and stream its progress.

    Prints one line per AuditEvent and per finished Step. A Run that pauses at a gate prints
    the `cograil approve` command that continues it. Uses Anthropic (ANTHROPIC_API_KEY)
    unless --fake-script is given. A local and demo tool: --as is not authenticated, and
    DATABASE_URL is required.

    \b
    Exit codes: 0 completed; 1 error (invalid workspace, audience denied, missing
    DATABASE_URL or ANTHROPIC_API_KEY, tool not available, database unreachable); 2 usage
    error; 3 awaiting approval; 4 escalated; 5 failed.
    """
    await_command(_run(path, protocol, _principal_id(as_), fake_script))


async def _run(path: Path, protocol_name: str, principal_id: str, script: Path | None) -> None:
    workspace = _load(path)
    protocol, colleague = _pick(workspace, protocol_name)
    principal = resolve_principal(workspace, principal_id)
    try:
        check_audience(workspace, colleague, protocol, principal)
    except AudienceDenied as exc:
        fail(f"denied: {exc}")
    provider = _provider(protocol, colleague, script)
    async with open_store() as base:
        store = ProgressStore(base, typer.echo)
        async with await _registry(workspace, store, path) as registry:
            _check_tools(workspace, protocol, registry)
            started = _new_run(workspace, path, protocol, colleague, principal)
            await store.create_run(started)
            typer.echo(f"run {started.id} ({protocol.name} as {principal.id})")
            runner = Runner(provider, registry, store, colleague, harness=workspace.harness)
            ended = await _guarded(runner.run(started.id, protocol))
            await _report(store, colleague, ended)


async def _guarded(running: Awaitable[Run]) -> Run:
    """The Runner's result; a failed Run (closed by the Runner) exits with EXIT_FAILED."""
    try:
        return await running
    except CograilError as exc:
        fail(f"failed: {type(exc).__name__}: {exc}", EXIT_FAILED)


async def _report(store: RunStore, colleague: Colleague, ended: Run) -> None:
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


@app.command()
def approve(
    token: Annotated[str, typer.Argument(help="The Approval token a paused Run printed.")],
    as_: AsOption,
    workspace: Annotated[
        Path | None,
        typer.Option(help="Workspace folder; default is the one the Run was started from."),
    ] = None,
    decline: Annotated[bool, typer.Option("--decline", help="Decline instead of approve.")] = False,
    fake_script: FakeScript = None,
) -> None:
    """Decide the Approval a Run is paused on; approved, the Run goes on at the paused Step.

    Only the Approval's approver may decide it, never the Run's own principal; anyone else is
    refused and the attempt is audited. Declining, or an Approval past its expiry, escalates the
    Run. Approving a Run whose harness.yaml, tools.yaml or python tool modules changed since it
    started is refused; it can still be declined. With --fake-script, the plans are those for
    the rest of the Run. A local and demo tool: --as is not authenticated, and DATABASE_URL is
    required.

    \b
    Exit codes: 0 completed; 1 error (unknown token, refused decision, Run not paused, missing
    DATABASE_URL, database unreachable, workspace changed or missing); 2 usage error; 3 paused
    again at another gate; 4 escalated; 5 failed.
    """
    await_command(_approve(token, _principal_id(as_), workspace, decline, fake_script))


async def _approve(
    token: str, decider: str, path: Path | None, decline: bool, script: Path | None
) -> None:
    async with open_store() as base:
        store = ProgressStore(base, typer.echo)
        try:
            paused = await store.get_run((await store.get_approval(token)).run_id)
        except (ApprovalNotFound, RunNotFound) as exc:
            fail(f"no such approval: {exc}")
        recorded = paused.context.get("workspace_path")
        if path is None and not recorded:
            fail("the Run records no workspace folder; pass --workspace")
        where = path or Path(str(recorded))
        workspace = _load(where)
        protocol, colleague = _pick(workspace, paused.protocol)
        if protocol.version != paused.protocol_version:
            fail(f"protocol {protocol.name} is now version {protocol.version}; the Run has "
                 f"version {paused.protocol_version}")  # fmt: skip
        provider = FakeProvider([]) if decline else _provider(protocol, colleague, script)
        async with await _registry(workspace, store, where) as registry:
            runner = Runner(provider, registry, store, colleague, harness=workspace.harness)
            decision: Literal["approved", "declined"] = "declined" if decline else "approved"
            canonical = resolve_principal(workspace, decider).id
            ended = await _decide(
                runner.resume(token, protocol, decider=canonical, decision=decision)
            )
            await _report(store, colleague, ended)


async def _decide(deciding: Awaitable[Run]) -> Run:
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


@asynccontextmanager
async def open_store() -> AsyncIterator[RunStore]:
    """The Postgres RunStore named by DATABASE_URL (a SQLAlchemy URL, postgresql+asyncpg://...)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        fail("DATABASE_URL is not set")
    store = PostgresRunStore.from_url(url)
    try:
        yield store
    finally:
        await store.dispose()


@app.command()
def runs(
    run_id: Annotated[str | None, typer.Argument(help="Show only this Run.")] = None,
    ledger: Annotated[bool, typer.Option("--ledger", help="Show the Window Ledger.")] = False,
    limit: Annotated[int, typer.Option(help="How many Runs to list.", min=1)] = 20,
) -> None:
    """List Runs, newest first; with --ledger, the tokens by source of each Step.

    \b
    Exit codes: 0 listed; 1 error (unknown Run, missing DATABASE_URL, database unreachable);
    2 usage error.
    """
    await_command(_show_runs(run_id, ledger, limit))


async def _show_runs(run_id: str | None, ledger: bool, limit: int) -> None:
    async with open_store() as store:
        try:
            found = [await store.get_run(run_id)] if run_id else await store.list_runs(limit)
        except RunNotFound as exc:
            fail(f"no such run: {exc}")
    for each in found:
        typer.echo(f"{each.id}  {each.status}  {each.protocol}  ${each.cost_usd:.4f}")
        if ledger:
            for line in format_ledger(window_ledger(each)):
                typer.echo(line)


@app.command()
def decide(
    table: Annotated[str, typer.Argument(help="Decision table name: decisions/<table>.yaml.")],
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", help="An input as name=value, read as its declared type; repeat."),
    ] = None,
    workspace: Annotated[Path, typer.Option(help="Workspace folder.")] = Path("."),
) -> None:
    """Evaluate a decision table by hand: print the rules that fired and their outputs.

    For testing a table: no Run, no model and no AuditEvent.

    \b
    Exit codes: 0 evaluated; 1 error (invalid workspace, unknown table, bad or missing input,
    no rule matched under hit_policy first); 2 usage error.
    """
    loaded = _load(workspace)
    try:
        found = table_for(loaded.decisions, table)
        outcome = found.evaluate(parse_inputs(found.decision, inputs or []))
    except DecisionError as exc:
        fail(f"invalid: {exc}")
    typer.echo(f"{outcome.table} v{outcome.version} ({outcome.hit_policy})")
    for rule, outputs in outcome.matches:
        typer.echo(f"rule {rule}: {json.dumps(outputs)}")
    if not outcome.matches:
        typer.echo("no rule matched")
