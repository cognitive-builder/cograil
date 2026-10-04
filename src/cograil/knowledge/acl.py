"""Which groups may see a document: a sidecar file, a folder file, or the source default.

Nearest wins: `<file>.acl.yaml` beside a document, else the `.acl.yaml` of the closest folder
at or above it inside the source, else the KnowledgeSource's own `acl_groups`. Each ACL file
is `acl_groups: [group, ...]`. An ACL file only narrows: a document gets the groups its file
names that the source also names, so a file dropped in the content folder cannot grant access
the source never declared. An unreadable or empty ACL file, or one that shares no group with
the source, is an error, never "no limit" or "no one": a mistake must not quietly widen or
hide a document.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from cograil.errors import KnowledgeSourceError

FOLDER_ACL = ".acl.yaml"
SIDECAR_SUFFIX = ".acl.yaml"


def groups_for(document: Path, source_root: Path, default: list[str]) -> list[str]:
    """The acl_groups of `document`, which lies under `source_root`; `default` is the source's."""
    sidecar = document.with_name(document.name + SIDECAR_SUFFIX)
    if sidecar.is_file():
        return _narrow(sidecar, source_root, default)
    folder = document.parent
    while True:
        candidate = folder / FOLDER_ACL
        if candidate.is_file():
            return _narrow(candidate, source_root, default)
        if folder == source_root or folder == folder.parent:
            return list(default)
        folder = folder.parent


def _narrow(path: Path, source_root: Path, default: list[str]) -> list[str]:
    allowed = set(default)
    groups = [g for g in _read_groups(path, source_root) if g in allowed]
    if not groups:
        raise KnowledgeSourceError(
            f"{path} names none of the source's acl_groups {default}; an ACL file only narrows"
        )
    return groups


def _read_groups(path: Path, source_root: Path) -> list[str]:
    if not path.resolve().is_relative_to(source_root.resolve()):
        raise KnowledgeSourceError(f"{path} points outside {source_root}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise KnowledgeSourceError(f"cannot read {path}: {exc}") from exc
    groups = data.get("acl_groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not groups:
        raise _bad(path)
    if not all(isinstance(g, str) and g.strip() for g in groups):
        raise _bad(path)
    return list(dict.fromkeys(g.strip() for g in groups))


def _bad(path: Path) -> KnowledgeSourceError:
    return KnowledgeSourceError(f"{path} needs `acl_groups:` with a non-empty list of names")
