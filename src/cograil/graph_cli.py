"""The `cograil graph` command: a Protocol's compiled graph as Mermaid (ADR 0011)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cograil.cliexit import fail
from cograil.errors import WorkspaceError
from cograil.graph import compile_graph, render_mermaid
from cograil.workspace import load_workspace


def graph(
    path: Annotated[Path, typer.Argument(help="Workspace folder.")],
    protocol: Annotated[str, typer.Option(help="Name of the Protocol to draw.")],
) -> None:
    """Print a Protocol's compiled graph as a Mermaid flowchart.

    Shows a node per Step, a gate node where a Step needs an Approval, a subgraph per helper
    Protocol, and dotted edges to `escalated` for declared failure thresholds and declined or
    expired Approvals. Paste the output into any Mermaid viewer. No Run, no model, no database.

    \b
    Exit codes: 0 printed; 1 error (invalid workspace, unknown protocol or helper, a Step
    naming an unknown Tool); 2 usage error.
    """
    try:
        workspace = load_workspace(path)
        found = next((p for p in workspace.protocols if p.name == protocol), None)
        if found is None:
            fail(f"no protocol {protocol!r} in {workspace.name}")
        typer.echo(render_mermaid(compile_graph(workspace, found)), nl=False)
    except WorkspaceError as exc:
        fail(f"invalid: {exc}")
