"""Turn a KnowledgeSource folder into Chunks and store them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from cograil.domain import Chunk, KnowledgeSource, Workspace
from cograil.errors import KnowledgeSourceError
from cograil.knowledge.acl import groups_for
from cograil.knowledge.chunking import chunk_text
from cograil.knowledge.loaders import find_documents, read_document
from cograil.knowledge.store import KnowledgeStore, SyncCounts
from cograil.observability import log_event


@dataclass(frozen=True)
class SourceReport:
    source: str
    files: int
    chunks: int
    counts: SyncCounts


def build_chunks(root: Path, source: KnowledgeSource) -> tuple[int, list[Chunk]]:
    """The number of documents in the source's folder and their Chunks, in a stable order.

    `root` is the workspace folder; the source's `path` must stay inside it. Blocking: it reads
    files, so async callers run it in a thread. A chunk's id is `<source>:<uri>#<n>`, so the same
    file always yields the same ids and a sync can tell new, changed and removed apart.
    """
    base = root.resolve()
    folder = (base / source.path).resolve()
    if not folder.is_relative_to(base):
        raise KnowledgeSourceError(f"{source.name}: path {source.path!r} is outside the workspace")
    if not folder.is_dir():
        raise KnowledgeSourceError(f"{source.name}: {source.path!r} is not a folder")
    documents = find_documents(folder)
    chunks: list[Chunk] = []
    for document in documents:
        uri = document.relative_to(base).as_posix()
        groups = groups_for(document, folder, source.acl_groups)
        pieces = chunk_text(read_document(document), source.chunk_size, source.chunk_overlap)
        if not pieces:
            log_event("knowledge.empty_document", source=source.name, source_uri=uri)
        chunks += [
            Chunk(id=f"{source.name}:{uri}#{n}", source=source.name, source_uri=uri,
                  text=piece, acl_groups=groups)
            for n, piece in enumerate(pieces)
        ]  # fmt: skip
    return len(documents), chunks


async def sync_source(root: Path, source: KnowledgeSource, store: KnowledgeStore) -> SourceReport:
    files, chunks = await asyncio.to_thread(build_chunks, root, source)
    counts = await store.sync_chunks(source.name, chunks)
    log_event("knowledge.synced", source=source.name, files=files, chunks=len(chunks))
    return SourceReport(source.name, files, len(chunks), counts)


async def sync_workspace(
    root: Path, workspace: Workspace, store: KnowledgeStore
) -> list[SourceReport]:
    """Sync every KnowledgeSource of the workspace, in declared order. The first source that
    cannot be loaded raises KnowledgeSourceError; sources before it stay synced."""
    return [await sync_source(root, source, store) for source in workspace.knowledge]
