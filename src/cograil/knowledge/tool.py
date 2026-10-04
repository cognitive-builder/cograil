"""The `knowledge` Tool kind: search a Workspace's KnowledgeSources as the calling principal.

Arguments: `query`, non-empty text, and an optional `limit` from 1 to 20 (default 5). The
groups searched as come from the CallContext, the Run's principal, never from the arguments,
so the model cannot widen what it sees. The result is `{"results": [...]}`, most similar first;
each result names its `source`, `source_uri` and `chunk_id` and quotes the Chunk's text
verbatim as `passage`, so an answer can cite where it came from. Like any Tool output, the
results reach the model as data, never as instructions, after the injection defence
(injection.py) has stripped and screened them; the recorded result keeps the passage verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cograil.domain import Workspace
from cograil.errors import ToolArgumentError
from cograil.knowledge.search import ScoredChunk
from cograil.knowledge.store import KnowledgeStore
from cograil.registry import ToolRegistry

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


class KnowledgeSearch:
    """The EntitledInvoke of one `knowledge` Tool over the given sources."""

    def __init__(self, name: str, store: KnowledgeStore, sources: Sequence[str]) -> None:
        self._name = name
        self._store = store
        self._sources = list(sources)

    async def __call__(self, args: dict[str, Any], groups: tuple[str, ...]) -> dict[str, Any]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentError(f"{self._name}: query: needs non-empty text")
        limit = args.get("limit", DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ToolArgumentError(f"{self._name}: limit: needs a whole number from 1 to 20")
        found = await self._store.search(query, groups, self._sources, limit)
        return {"results": [_cited(each) for each in found]}


def _cited(found: ScoredChunk) -> dict[str, Any]:
    chunk = found.chunk
    return {
        "source": chunk.source,
        "source_uri": chunk.source_uri,
        "chunk_id": chunk.id,
        "passage": chunk.text,
        "score": round(found.score, 4),
    }


def add_knowledge(registry: ToolRegistry, workspace: Workspace, store: KnowledgeStore) -> None:
    """Register every `knowledge` Tool of the workspace; each searches all its sources."""
    sources = [source.name for source in workspace.knowledge]
    for tool in workspace.tools:
        if tool.kind == "knowledge":
            registry.register_entitled(tool, KnowledgeSearch(tool.name, store, sources))
