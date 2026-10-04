"""Load a workspace folder into a Workspace (ADR 0004).

Layout: tools.yaml, colleagues/*.yaml and protocols/*.md are required;
connections.yaml, audiences.yaml and knowledge.yaml are optional and default to empty;
harness.yaml is optional and defaults to the Harness defaults (ADR 0012).
Every failure is a WorkspaceError whose message names the offending file.
"""

from __future__ import annotations

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
    Protocol,
    Tool,
    Workspace,
)
from cograil.errors import ProtocolParseError, WorkspaceError
from cograil.parser import parse_protocol


def load_workspace(path: Path | str) -> Workspace:
    """Load and validate the workspace at path; raise WorkspaceError on any problem."""
    root = Path(path)
    if not root.is_dir():
        raise WorkspaceError(f"{root}: workspace folder not found")
    tools = _load_list(root / "tools.yaml", "tools", Tool, required=True)
    return Workspace(
        name=root.resolve().name,
        colleagues=_load_colleagues(root / "colleagues"),
        protocols=_load_protocols(root / "protocols", {tool.name for tool in tools}),
        tools=tools,
        connections=_load_list(root / "connections.yaml", "connections", Connection),
        audiences=_load_list(root / "audiences.yaml", "audiences", Audience),
        knowledge=_load_list(root / "knowledge.yaml", "knowledge", KnowledgeSource),
        harness=_load_harness(root / "harness.yaml"),
    )


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
    file: Path, key: str, model: type[M], required: bool = False
) -> list[M]:
    if not file.is_file():
        if required:
            raise WorkspaceError(f"{file}: required file is missing")
        return []
    data = _read_yaml(file)
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise WorkspaceError(f"{file}: expected a top-level '{key}' list")
    return [_build(model, item, file) for item in items]


def _files(folder: Path, pattern: str) -> list[Path]:
    if not folder.is_dir():
        raise WorkspaceError(f"{folder}: required folder is missing")
    files = sorted(folder.glob(pattern))
    if not files:
        raise WorkspaceError(f"{folder}: required folder has no {pattern} files")
    return files


def _load_colleagues(folder: Path) -> list[Colleague]:
    return [_build(Colleague, _read_yaml(f), f) for f in _files(folder, "*.yaml")]


def _load_protocols(folder: Path, tool_names: set[str]) -> list[Protocol]:
    protocols = []
    for file in _files(folder, "*.md"):
        try:
            protocols.append(parse_protocol(file.read_text(), known_tools=tool_names))
        except (OSError, UnicodeDecodeError, ProtocolParseError) as exc:
            raise WorkspaceError(f"{file}: {exc}") from exc
    return protocols
