"""The `cograil eval` command: run a workspace's golden set and report (issue #30)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from cograil.cliexit import await_command, fail
from cograil.domain import Workspace
from cograil.errors import CograilError, WorkspaceError
from cograil.evals.cases import EvalCase, load_cases
from cograil.evals.report import format_report
from cograil.evals.suite import Level, choose_level, level_harness, run_suite
from cograil.providers import FakeProvider, Provider, make_provider, provider_ready
from cograil.workspace import load_workspace

LIVE_ENV = "COGRAIL_LIVE"
CASES_DIR = Path("tests/evals")


def eval_command(
    path: Annotated[Path, typer.Argument(help="Workspace folder.")],
    cases: Annotated[
        Path | None,
        typer.Option(help="JSONL golden set; default tests/evals/<workspace name>.jsonl."),
    ] = None,
    level: Annotated[
        Level | None,
        typer.Option(help="fake (default), or with COGRAIL_LIVE=1 smoke (default) or full."),
    ] = None,
) -> None:
    """Run a workspace's eval cases and report each case's outcome, tokens and cost.

    Without COGRAIL_LIVE=1 every case runs on the FakeProvider, replaying its script, at $0.
    With COGRAIL_LIVE=1 the cases run on the live provider (ANTHROPIC_API_KEY): `smoke` runs
    the cases marked smoke with every tier on the small tier's model; `full` runs every case a
    real model can play, and only through the provider's batch path (not built yet, issue #55).
    The report ends with the cost per resolved run of each Protocol (ADR 0013). No database.

    \b
    Exit codes: 0 every case passed; 1 a case failed, or an error (invalid workspace or case
    file, a live level without COGRAIL_LIVE=1, the full level, missing ANTHROPIC_API_KEY);
    2 usage error.
    """
    live = os.environ.get(LIVE_ENV) == "1"
    try:
        chosen = choose_level(level, live)
    except CograilError as exc:
        fail(str(exc))
    workspace = _load(path)
    try:
        golden = load_cases(cases or CASES_DIR / f"{workspace.name}.jsonl")
    except CograilError as exc:
        fail(f"invalid: {exc}")
    await_command(_eval(workspace, path, golden, chosen))


def _load(path: Path) -> Workspace:
    try:
        return load_workspace(path)
    except WorkspaceError as exc:
        fail(f"invalid: {exc}")


async def _eval(workspace: Workspace, path: Path, cases: list[EvalCase], level: Level) -> None:
    provider_for = _providers(workspace, level)
    try:
        results = await run_suite(workspace, path, cases, level, provider_for)
    except CograilError as exc:
        fail(f"error: {type(exc).__name__}: {exc}")
    for line in format_report(workspace.name, level, results):
        typer.echo(line)
    if not all(result.passed for result in results):
        raise typer.Exit(code=1)


def _providers(workspace: Workspace, level: Level) -> Callable[[EvalCase], Provider]:
    """A fresh FakeProvider per case on its script, or the live provider of the level's tiers."""
    if level is Level.fake:
        return lambda case: FakeProvider(case.plans())
    harness = level_harness(workspace.harness, level)
    if not provider_ready(harness):
        fail("ANTHROPIC_API_KEY is not set (or leave COGRAIL_LIVE unset for the fake level)")
    try:
        live = make_provider(harness, harness.models.standard)
    except CograilError as exc:
        fail(str(exc))
    return lambda case: live
