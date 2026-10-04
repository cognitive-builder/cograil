"""KnowledgeStore: where the Chunks of each KnowledgeSource are kept.

`sync_chunks` makes a source hold exactly the Chunks given: it adds new ones, rewrites changed
ones, removes the rest and leaves equal ones alone, so a second sync of unchanged files
writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import Chunk
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


@dataclass(frozen=True)
class _Plan:
    write: list[Chunk]
    remove: list[str]
    counts: SyncCounts


def _plan(existing: Mapping[str, Chunk], wanted: Sequence[Chunk]) -> _Plan:
    write = [c for c in wanted if existing.get(c.id) != c]
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


def _upsert(write: Sequence[Chunk]) -> Insert:
    statement = insert(chunks_table).values([c.model_dump() for c in write])
    changes = {name: statement.excluded[name] for name in Chunk.model_fields if name != "id"}
    return statement.on_conflict_do_update(index_elements=["id"], set_=changes)


class InMemoryKnowledgeStore:
    """Dict-backed KnowledgeStore for unit tests and local runs without a database."""

    def __init__(self) -> None:
        self._chunks: dict[str, dict[str, Chunk]] = {}

    async def sync_chunks(self, source: str, chunks: Sequence[Chunk]) -> SyncCounts:
        held = self._chunks.setdefault(source, {})
        plan = _plan(held, chunks)
        for chunk in plan.write:
            held[chunk.id] = chunk.model_copy(deep=True)
        for chunk_id in plan.remove:
            del held[chunk_id]
        return plan.counts

    async def list_chunks(self, source: str) -> list[Chunk]:
        held = self._chunks.get(source, {})
        return [held[i].model_copy(deep=True) for i in sorted(held)]


class PostgresKnowledgeStore:
    """KnowledgeStore on Postgres through SQLAlchemy async Core. Schema comes from Alembic."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str) -> PostgresKnowledgeStore:
        """`url` is a SQLAlchemy URL such as postgresql+asyncpg://user:pw@host/db."""
        return cls(create_async_engine(url))

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def sync_chunks(self, source: str, chunks: Sequence[Chunk]) -> SyncCounts:
        table = chunks_table
        async with self._engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.source == source))).mappings()
            plan = _plan({r["id"]: Chunk.model_validate(dict(r)) for r in rows}, chunks)
            for batch in _batches(plan.write):
                await conn.execute(_upsert(batch))
            for ids in _batches(plan.remove):
                await conn.execute(delete(table).where(table.c.id.in_(ids)))
        return plan.counts

    async def list_chunks(self, source: str) -> list[Chunk]:
        table = chunks_table
        query = select(table).where(table.c.source == source).order_by(table.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [Chunk.model_validate(dict(r)) for r in rows]


__all__ = [
    "InMemoryKnowledgeStore",
    "KnowledgeStore",
    "PostgresKnowledgeStore",
    "SyncCounts",
]
