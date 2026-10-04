"""The cograil command line. `run` and `approve` arrive with issue #13."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cograil.errors import WorkspaceError
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
