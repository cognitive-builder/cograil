"""Load a workspace folder into a Workspace (ADR 0004).

Layout: tools.yaml, colleagues/*.yaml and protocols/*.md are required;
connections.yaml, audiences.yaml, knowledge.yaml and principals.yaml are optional and default
to empty; harness.yaml is optional and defaults to the Harness defaults (ADR 0012).
Every failure is a WorkspaceError whose message names the offending file. `check_workspace`
keeps going after a failure and reports every problem it can find; `load_workspace` raises one
WorkspaceError carrying all of them, one per line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from cograil.domain import (
    Audience,
    Colleague,
    Connection,
    Harness,
    KnowledgeSource,
    Principal,
    Protocol,
    Tool,
    Workspace,
)
from cograil.errors import ProtocolParseError, WorkspaceError
from cograil.parser import parse_protocol


@dataclass(frozen=True)
class WorkspaceReport:
    """What a check found. `workspace` holds whatever loaded; it is whole only if no problems."""

    workspace: Workspace
    problems: list[str]


class _Problems:
    def __init__(self) -> None:
        self.found: list[str] = []

    def attempt[**P, T](self, load: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T | None:
        """The result of load, or None after recording the WorkspaceError it raised."""
        try:
            return load(*args, **kwargs)
        except WorkspaceError as exc:
            self.found.append(str(exc))
            return None


def load_workspace(path: Path | str) -> Workspace:
    """Load and validate the workspace at path; raise WorkspaceError on any problem."""
    report = check_workspace(path)
    if report.problems:
        raise WorkspaceError("\n".join(report.problems))
    return report.workspace


def check_workspace(path: Path | str) -> WorkspaceReport:
    """Load the workspace at path, collecting every problem instead of stopping at the first."""
    root = Path(path)
    problems = _Problems()
    if not root.is_dir():
        problems.found.append(f"{root}: workspace folder not found")
        return WorkspaceReport(Workspace(name=root.resolve().name, colleagues=[], protocols=[],
                                         tools=[]), problems.found)  # fmt: skip
    before = len(problems.found)
    tools = _load_list(problems, root / "tools.yaml", "tools", Tool, required=True)
    # If any Tool failed to load the @refs cannot be checked, but the Steps still parse.
    known = {tool.name for tool in tools} if len(problems.found) == before else None
    workspace = Workspace(
        name=root.resolve().name,
        colleagues=_load_colleagues(problems, root / "colleagues"),
        protocols=_load_protocols(problems, root / "protocols", known),
        tools=tools,
        connections=_load_list(problems, root / "connections.yaml", "connections", Connection),
        audiences=_load_list(problems, root / "audiences.yaml", "audiences", Audience),
        knowledge=_load_list(problems, root / "knowledge.yaml", "knowledge", KnowledgeSource),
        principals=_load_list(problems, root / "principals.yaml", "principals", Principal),
        harness=problems.attempt(_load_harness, root / "harness.yaml") or Harness(),
    )
    return WorkspaceReport(workspace, problems.found)


def _load_harness(file: Path) -> Harness:
    if not file.is_file():
        return Harness()
    return _build(Harness, _read_yaml(file) or {}, file)


def _read_yaml(file: Path) -> Any:
    try:
        return yaml.safe_load(file.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise WorkspaceError(f"{file}: cannot read YAML: {exc}") from exc


def _build[M: BaseModel](model: type[M], data: object, file: Path) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise WorkspaceError(f"{file}: invalid {model.__name__}: {exc}") from exc


def _load_list[M: BaseModel](
    problems: _Problems, file: Path, key: str, model: type[M], required: bool = False
) -> list[M]:
    if not file.is_file():
        if required:
            problems.found.append(f"{file}: required file is missing")
        return []
    items = problems.attempt(_list_items, file, key)
    built = (problems.attempt(_build, model, item, file) for item in items or [])
    return [item for item in built if item is not None]


def _list_items(file: Path, key: str) -> list[Any]:
    data = _read_yaml(file)
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise WorkspaceError(f"{file}: expected a top-level '{key}' list")
    return items


def _files(folder: Path, pattern: str) -> list[Path]:
    if not folder.is_dir():
        raise WorkspaceError(f"{folder}: required folder is missing")
    files = sorted(folder.glob(pattern))
    if not files:
        raise WorkspaceError(f"{folder}: required folder has no {pattern} files")
    return files


def _load_colleagues(problems: _Problems, folder: Path) -> list[Colleague]:
    files = problems.attempt(_files, folder, "*.yaml") or []
    loaded = (problems.attempt(_colleague, file) for file in files)
    return [colleague for colleague in loaded if colleague is not None]


def _colleague(file: Path) -> Colleague:
    return _build(Colleague, _read_yaml(file), file)


def _load_protocols(
    problems: _Problems, folder: Path, tool_names: set[str] | None
) -> list[Protocol]:
    files = problems.attempt(_files, folder, "*.md") or []
    loaded = (problems.attempt(_protocol, file, tool_names) for file in files)
    return [protocol for protocol in loaded if protocol is not None]


def _protocol(file: Path, tool_names: set[str] | None) -> Protocol:
    try:
        return parse_protocol(file.read_text(), known_tools=tool_names)
    except (OSError, UnicodeDecodeError, ProtocolParseError) as exc:
        raise WorkspaceError(f"{file}: {exc}") from exc
