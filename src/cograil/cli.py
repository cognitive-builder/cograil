"""The cograil command line. `run` and `approve` arrive with issue #13."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import typer

from cograil.context import format_ledger, window_ledger
from cograil.errors import RunNotFound, WorkspaceError
from cograil.store import PostgresRunStore, RunStore
from cograil.workspace import load_workspace

app = typer.Typer(help="Cograil: run Markdown runbooks as an AI colleague.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Keep `validate` a named subcommand while it is the only one."""


@app.command()
def validate(
    path: Annotated[Path, typer.Argument(help="Workspace folder to validate.")],
) -> None:
    """Load a workspace and exit non-zero when it is invalid."""
    try:
        workspace = load_workspace(path)
    except WorkspaceError as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"ok: {workspace.name} ({len(workspace.colleagues)} colleagues, "
        f"{len(workspace.protocols)} protocols, {len(workspace.tools)} tools)"
    )


@asynccontextmanager
async def open_store() -> AsyncIterator[RunStore]:
    """The Postgres RunStore named by DATABASE_URL (a SQLAlchemy URL, postgresql+asyncpg://...)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        typer.echo("DATABASE_URL is not set", err=True)
        raise typer.Exit(code=1)
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
    """List Runs, newest first; with --ledger, the tokens by source of each Step."""
    asyncio.run(_show_runs(run_id, ledger, limit))


async def _show_runs(run_id: str | None, ledger: bool, limit: int) -> None:
    async with open_store() as store:
        try:
            found = [await store.get_run(run_id)] if run_id else await store.list_runs(limit)
        except RunNotFound as exc:
            typer.echo(f"no such run: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    for run in found:
        typer.echo(f"{run.id}  {run.status}  {run.protocol}  ${run.cost_usd:.4f}")
        if ledger:
            for line in format_ledger(window_ledger(run)):
                typer.echo(line)
