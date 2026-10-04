"""KnowledgeStore: where the Chunks of each KnowledgeSource are kept and searched.

`sync_chunks` makes a source hold exactly the Chunks given: it adds new ones, rewrites changed
ones, removes the rest and leaves equal ones alone, so a second sync of unchanged files
writes nothing. Each Chunk written is embedded by the store's Embedder; a Postgres Chunk
stored before it had an embedding counts as changed. `search` returns the Chunks a set of
groups may see, most similar first (search.py).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import RowMapping, delete, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import Chunk
from cograil.errors import KnowledgeSourceError
from cograil.knowledge.embedding import Embedder, HashingEmbedder
from cograil.knowledge.search import ScoredChunk, entitled, rank, search_statement
from cograil.store_tables import EMBEDDING_DIMENSIONS
from cograil.store_tables import chunks as chunks_table


@dataclass(frozen=True)
class SyncCounts:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


@runtime_checkable
class KnowledgeStore(Protocol):
    async def sync_chunks(self, source: str, chunks: Sequence[Chunk]) -> SyncCounts:
        """Make `source` hold exactly `chunks`, all of which belong to it."""
        ...

    async def list_chunks(self, source: str) -> list[Chunk]:
        """The Chunks of `source`, ordered by id."""
        ...

    async def search(
        self, query: str, groups: Sequence[str], sources: Sequence[str], limit: int
    ) -> list[ScoredChunk]:
        """At most `limit` Chunks of `sources` that name one of `groups`, most similar to
        `query` first. Only Chunks the groups may see are ranked at all."""
        ...


@dataclass(frozen=True)
class _Plan:
    write: list[Chunk]
    remove: list[str]
    counts: SyncCounts


def _plan(
    existing: Mapping[str, Chunk], wanted: Sequence[Chunk], stale: frozenset[str] = frozenset()
) -> _Plan:
    write = [c for c in wanted if c.id in stale or existing.get(c.id) != c]
    wanted_ids = {c.id for c in wanted}
    remove = sorted(set(existing) - wanted_ids)
    added = sum(1 for c in write if c.id not in existing)
    counts = SyncCounts(
        added=added,
        updated=len(write) - added,
        removed=len(remove),
        unchanged=len(wanted) - len(write),
    )
    return _Plan(write, remove, counts)


_BATCH = 1000  # asyncpg takes at most 32767 parameters per statement


def _batches[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), _BATCH):
        yield items[start : start + _BATCH]


def _upsert(write: Sequence[tuple[Chunk, list[float]]]) -> Insert:
    rows = [c.model_dump() | {"embedding": v} for c, v in write]
    statement = insert(chunks_table).values(rows)
    names = [*Chunk.model_fields, "embedding"]
    changes = {name: statement.excluded[name] for name in names if name != "id"}
    return statement.on_conflict_do_update(index_elements=["id"], set_=changes)


def _chunk_of(row: RowMapping) -> Chunk:
    return Chunk.model_validate({name: row[name] for name in Chunk.model_fields})


def _nothing_to_search(groups: Sequence[str], sources: Sequence[str], limit: int) -> bool:
    return not groups or not sources or limit < 1


def _embedder_or_default(embedder: Embedder | None) -> Embedder:
    """The given Embedder, or HashingEmbedder. Both stores write `chunks.embedding`, so any
    embedder they take must give exactly its width."""
    chosen = embedder or HashingEmbedder()
    if chosen.dimensions != EMBEDDING_DIMENSIONS:
        raise KnowledgeSourceError(
            f"the embedder gives {chosen.dimensions} dimensions; "
            f"chunks.embedding holds {EMBEDDING_DIMENSIONS}"
        )
    return chosen


class InMemoryKnowledgeStore:
    """Dict-backed KnowledgeStore for unit tests and local runs without a database."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = _embedder_or_default(embedder)
        self._chunks: dict[str, dict[str, tuple[Chunk, list[float]]]] = {}

    async def sync_chunks(self, source: str, chunks: Sequence[Chunk]) -> SyncCounts:
        held = self._chunks.setdefault(source, {})
        plan = _plan({i: c for i, (c, _) in held.items()}, chunks)
        vectors = await self._embedder.embed([c.text for c in plan.write])
        for chunk, vector in zip(plan.write, vectors, strict=True):
            held[chunk.id] = (chunk.model_copy(deep=True), vector)
        for chunk_id in plan.remove:
            del held[chunk_id]
        return plan.counts

    async def list_chunks(self, source: str) -> list[Chunk]:
        held = self._chunks.get(source, {})
        return [held[i][0].model_copy(deep=True) for i in sorted(held)]

    async def search(
        self, query: str, groups: Sequence[str], sources: Sequence[str], limit: int
    ) -> list[ScoredChunk]:
        if _nothing_to_search(groups, sources, limit):
            return []
        allowed = [
            (chunk, vector)
            for source in dict.fromkeys(sources)
            for chunk, vector in self._chunks.get(source, {}).values()
            if entitled(chunk, groups)
        ]
        [vector] = await self._embedder.embed([query])
        return rank(allowed, vector, limit)


class PostgresKnowledgeStore:
    """KnowledgeStore on Postgres through SQLAlchemy async Core. Schema comes from Alembic."""

    def __init__(self, engine: AsyncEngine, embedder: Embedder | None = None) -> None:
        self._engine = engine
        self._embedder = _embedder_or_default(embedder)

    @classmethod
    def from_url(cls, url: str, embedder: Embedder | None = None) -> PostgresKnowledgeStore:
        """`url` is a SQLAlchemy URL such as postgresql+asyncpg://user:pw@host/db."""
        return cls(create_async_engine(url), embedder)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def sync_chunks(self, source: str, chunks: Sequence[Chunk]) -> SyncCounts:
        table = chunks_table
        columns = [table.c[name] for name in Chunk.model_fields]
        query = select(*columns, table.c.embedding.is_(None).label("stale"))
        async with self._engine.begin() as conn:
            rows = (await conn.execute(query.where(table.c.source == source))).mappings().all()
            existing = {r["id"]: _chunk_of(r) for r in rows}
            plan = _plan(existing, chunks, frozenset(r["id"] for r in rows if r["stale"]))
            vectors = await self._embedder.embed([c.text for c in plan.write])
            for batch in _batches(list(zip(plan.write, vectors, strict=True))):
                await conn.execute(_upsert(batch))
            for ids in _batches(plan.remove):
                await conn.execute(delete(table).where(table.c.id.in_(ids)))
        return plan.counts

    async def list_chunks(self, source: str) -> list[Chunk]:
        table = chunks_table
        columns = [table.c[name] for name in Chunk.model_fields]
        query = select(*columns).where(table.c.source == source).order_by(table.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_chunk_of(r) for r in rows]

    async def search(
        self, query: str, groups: Sequence[str], sources: Sequence[str], limit: int
    ) -> list[ScoredChunk]:
        if _nothing_to_search(groups, sources, limit):
            return []
        [vector] = await self._embedder.embed([query])
        if not any(vector):
            return []  # a query without words is similar to nothing
        statement = search_statement(vector, groups, sources, limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [ScoredChunk(_chunk_of(r), min(1.0, 1.0 - r["distance"])) for r in rows]


__all__ = [
    "InMemoryKnowledgeStore",
    "KnowledgeStore",
    "PostgresKnowledgeStore",
    "ScoredChunk",
    "SyncCounts",
]
