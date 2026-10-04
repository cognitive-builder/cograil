"""`cograil validate`: every problem a workspace has, found before any Run.

The loader's problems come first (workspace.py). On top of them, each Step's whitelisted
Tools must have distinct Anthropic wire names (`a.b` and `a__b` collide), the same check the
provider makes mid-Run, so a workspace author finds out here instead.
"""

from __future__ import annotations

from pathlib import Path

from cograil.domain import Workspace
from cograil.errors import ProviderError
from cograil.providers.wire import wire_names
from cograil.workspace import check_workspace


def validate_workspace(path: Path | str) -> list[str]:
    """All problems of the workspace at path; empty when it is valid."""
    report = check_workspace(path)
    return [*report.problems, *_wire_name_problems(report.workspace)]


def _wire_name_problems(workspace: Workspace) -> list[str]:
    by_name = {tool.name: tool for tool in workspace.tools}
    problems = []
    for protocol in workspace.protocols:
        for step in protocol.steps:
            tools = [by_name[name] for name in step.tools if name in by_name]
            try:
                wire_names(tools)
            except ProviderError as exc:
                problems.append(f"protocol {protocol.name}, step {step.number}: {exc}")
    return problems
