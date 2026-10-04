"""Load a workspace folder into a Workspace (ADR 0004).

Layout: tools.yaml, colleagues/*.yaml and protocols/*.md are required;
connections.yaml, audiences.yaml, knowledge.yaml, principals.yaml and decisions/*.yaml are
optional and default to empty; harness.yaml is optional and defaults to the Harness defaults
(ADR 0012). Each decision table must be named after its file and pass the checks of
decisions.py, and each `decision` Tool must name a table that loaded (ADR 0009). No principal
id or alias may name two principals once normalised (cograil.identity), and no Colleague's
escalation_contact may be an alias.
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

from cograil.decisions import DecisionTable, table_name
from cograil.domain import (
    Audience,
    Colleague,
    Connection,
    Decision,
    Harness,
    KnowledgeSource,
    Principal,
    Protocol,
    Tool,
    Workspace,
)
from cograil.errors import DecisionError, ProtocolParseError, WorkspaceError
from cograil.identity import normalise_principal_id
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
        decisions=_load_decisions(problems, root / "decisions"),
        principals=_load_list(problems, root / "principals.yaml", "principals", Principal),
        harness=problems.attempt(_load_harness, root / "harness.yaml") or Harness(),
    )
    problems.found.extend(_unknown_tables(workspace, root / "tools.yaml"))
    problems.found.extend(_shared_names(workspace, root / "principals.yaml"))
    problems.found.extend(_alias_contacts(workspace, root / "colleagues"))
    return WorkspaceReport(workspace, problems.found)


def _load_decisions(problems: _Problems, folder: Path) -> list[Decision]:
    files = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    loaded = (problems.attempt(_decision, file) for file in files)
    return [decision for decision in loaded if decision is not None]


def _decision(file: Path) -> Decision:
    decision = _build(Decision, _read_yaml(file), file)
    if decision.name != file.stem:
        raise WorkspaceError(f"{file}: the table is named {decision.name!r}, not {file.stem!r}")
    try:
        DecisionTable(decision)
    except DecisionError as exc:
        raise WorkspaceError(f"{file}: {exc}") from exc
    return decision


def _unknown_tables(workspace: Workspace, file: Path) -> list[str]:
    tables = {decision.name for decision in workspace.decisions}
    return [
        f"{file}: {tool.name} needs decisions/{table_name(tool)}.yaml"
        for tool in workspace.tools
        if tool.kind == "decision" and table_name(tool) not in tables
    ]


def _shared_names(workspace: Workspace, file: Path) -> list[str]:
    """An id or alias naming two principals would let one sign in as the other."""
    owners: dict[str, str] = {}
    shared: list[str] = []
    for principal in workspace.principals:
        for name in dict.fromkeys([principal.id, *principal.aliases]):
            if owners.setdefault(name, principal.id) != principal.id:
                shared.append(f"{file}: {name} names both {owners[name]} and {principal.id}")
    return shared


def _alias_contacts(workspace: Workspace, folder: Path) -> list[str]:
    """Gates compare ids, so an escalation_contact must be the principal's id, not an alias."""
    canonical = {alias: p.id for p in workspace.principals for alias in p.aliases}
    return [
        f"{folder}: {c.name} escalation_contact {contact} is an alias; use {canonical[contact]}"
        for c in workspace.colleagues
        if (contact := normalise_principal_id(c.escalation_contact)) in canonical
    ]


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
