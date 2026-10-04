"""Which groups may see a document: a sidecar file, a folder file, or the source default.

Nearest wins: `<file>.acl.yaml` beside a document, else the `.acl.yaml` of the closest folder
at or above it inside the source, else the KnowledgeSource's own `acl_groups`. Each ACL file
is `acl_groups: [group, ...]`. An unreadable or empty ACL file is an error, never "no limit"
or "no one": a mistake must not quietly widen or hide a document.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from cograil.errors import KnowledgeSourceError

FOLDER_ACL = ".acl.yaml"
SIDECAR_SUFFIX = ".acl.yaml"


def groups_for(document: Path, source_root: Path, default: list[str]) -> list[str]:
    """The acl_groups of `document`, which lies under `source_root`."""
    sidecar = document.with_name(document.name + SIDECAR_SUFFIX)
    if sidecar.is_file():
        return _read_groups(sidecar)
    folder = document.parent
    while True:
        candidate = folder / FOLDER_ACL
        if candidate.is_file():
            return _read_groups(candidate)
        if folder == source_root or folder == folder.parent:
            return list(default)
        folder = folder.parent


def _read_groups(path: Path) -> list[str]:
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
