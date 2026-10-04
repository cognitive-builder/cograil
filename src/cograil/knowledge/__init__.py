"""Knowledge: load a folder of Markdown and PDF into Chunks tagged with acl_groups."""

from cograil.knowledge.chunking import chunk_text
from cograil.knowledge.store import (
    InMemoryKnowledgeStore,
    KnowledgeStore,
    PostgresKnowledgeStore,
    SyncCounts,
)
from cograil.knowledge.sync import SourceReport, build_chunks, sync_source, sync_workspace

__all__ = [
    "InMemoryKnowledgeStore",
    "KnowledgeStore",
    "PostgresKnowledgeStore",
    "SourceReport",
    "SyncCounts",
    "build_chunks",
    "chunk_text",
    "sync_source",
    "sync_workspace",
]
