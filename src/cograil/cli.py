"""The cograil command line: validate, graph, eval, run, approve, runs, decide, knowledge sync.

`run` and `approve` are a local and demo tool. `--as` is taken at face value: nothing here
authenticates the principal, and the safety comes from needing DATABASE_URL. A principal's
groups come from the workspace's principals.yaml. `approve` goes through `Runner.resume`, so
the approver check and the `gate.refused` AuditEvent hold; real authentication of the decider
belongs to the web and Slack channels (issue #108).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import typer

from cograil.audience import check_audience, resolve_principal
from cograil.cli_support import (
    AsOption,
    FakeScript,
    build_tools,
    check_tools,
    guarded_decision,
    guarded_run,
    load_or_fail,
    make_run_provider,
    pick_protocol,
    principal_id,
    report_run,
)
from cograil.cliexit import EXIT_CODES, await_command, fail
from cograil.context import cache_ledger, compression_ledger, format_ledger, window_ledger
from cograil.cost import cost_by_protocol, format_cost_table
from cograil.decisions import parse_inputs, table_for
from cograil.errors import (
    ApprovalNotFound,
    AudienceDenied,
    DecisionError,
    RunNotFound,
)
from cograil.evals.cli import eval_command
from cograil.graph_cli import graph
from cograil.knowledge.cli import knowledge_app
from cograil.progress import ProgressStore
from cograil.providers import FakeProvider
from cograil.run_input import received_run
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
app.command(name="eval")(eval_command)


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


@app.command()
def run(
    path: Annotated[Path, typer.Argument(help="Workspace folder.")],
    protocol: Annotated[str, typer.Option(help="Name of the Protocol to run.")],
    as_: AsOption,
    message: Annotated[str, typer.Option(help="What the requester asks; every Step sees it.")],
    fake_script: FakeScript = None,
) -> None:
    """Start a Run of a Protocol with the requester's message and stream its progress.

    Prints one line per AuditEvent and per finished Step. A Run that pauses at a gate prints
    the `cograil approve` command that continues it. Uses Anthropic (ANTHROPIC_API_KEY)
    unless --fake-script is given. A local and demo tool: --as is not authenticated, and
    DATABASE_URL is required.

    \b
    Exit codes: 0 completed; 1 error (invalid workspace, audience denied, missing
    DATABASE_URL or ANTHROPIC_API_KEY, tool not available, database unreachable); 2 usage
    error; 3 awaiting approval; 4 escalated; 5 failed.
    """
    if not message.strip():
        raise typer.BadParameter("say what the Run is asked to do", param_hint="--message")
    await_command(_run(path, protocol, principal_id(as_), message, fake_script))


async def _run(
    path: Path, protocol_name: str, principal_id: str, message: str, script: Path | None
) -> None:
    workspace = load_or_fail(path)
    protocol, colleague = pick_protocol(workspace, protocol_name)
    principal = resolve_principal(workspace, principal_id)
    try:
        check_audience(workspace, colleague, protocol, principal)
    except AudienceDenied as exc:
        fail(f"denied: {exc}")
    provider = make_run_provider(workspace, script)
    async with open_store() as base:
        store = ProgressStore(base, typer.echo)
        async with await build_tools(workspace, store, path, script is None) as registry:
            check_tools(workspace, protocol, registry)
            # Trigger.kind has no "cli": a CLI Run is a chat on the channel "cli".
            started = received_run(
                workspace.name, path, protocol, colleague, principal, channel="cli", message=message
            )
            await store.create_run(started)
            typer.echo(f"run {started.id} ({protocol.name} as {principal.id})")
            runner = Runner(provider, registry, store, colleague, harness=workspace.harness)
            ended = await guarded_run(runner.run(started.id, protocol))
            await report_run(store, colleague, ended)


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
    await_command(_approve(token, principal_id(as_), workspace, decline, fake_script))


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
        workspace = load_or_fail(where)
        protocol, colleague = pick_protocol(workspace, paused.protocol)
        if protocol.version != paused.protocol_version:
            fail(f"protocol {protocol.name} is now version {protocol.version}; the Run has "
                 f"version {paused.protocol_version}")  # fmt: skip
        provider = FakeProvider([]) if decline else make_run_provider(workspace, script)
        async with await build_tools(workspace, store, where, script is None) as registry:
            runner = Runner(provider, registry, store, colleague, harness=workspace.harness)
            decision: Literal["approved", "declined"] = "declined" if decline else "approved"
            canonical = resolve_principal(workspace, decider).id
            ended = await guarded_decision(
                runner.resume(token, protocol, decider=canonical, decision=decision)
            )
            await report_run(store, colleague, ended)


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
    cost: Annotated[bool, typer.Option("--cost", help="Cost by Protocol, not Runs.")] = False,
    limit: Annotated[
        int,
        typer.Option(
            help="How many of the newest Runs to show; --cost aggregates over exactly these Runs, "
            "across all Protocols.",
            min=1,
        ),
    ] = 20,
) -> None:
    """List Runs, newest first; with --ledger, the tokens by source of each Step.
    With --cost, a table by Protocol: tokens by category, cost, cost per resolved run.

    \b
    Exit codes: 0 listed; 1 error (unknown Run, missing DATABASE_URL, database unreachable);
    2 usage error.
    """
    await_command(_show_runs(run_id, ledger, cost, limit))


async def _show_runs(run_id: str | None, ledger: bool, cost: bool, limit: int) -> None:
    async with open_store() as store:
        try:
            found = [await store.get_run(run_id)] if run_id else await store.list_runs(limit)
        except RunNotFound as exc:
            fail(f"no such run: {exc}")
    if cost:
        for line in format_cost_table(cost_by_protocol(found)):
            typer.echo(line)
        return
    for each in found:
        typer.echo(f"{each.id}  {each.status}  {each.protocol}  ${each.cost_usd:.4f}")
        if ledger:
            for line in format_ledger(
                window_ledger(each), compression_ledger(each), cache_ledger(each)
            ):
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
    loaded = load_or_fail(workspace)
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
