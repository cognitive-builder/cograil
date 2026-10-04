"""Knowledge search: the Chunks a principal may see, ranked by similarity to a query.

Entitlement comes first (product rule 3). The Postgres statement selects the Chunks whose
`acl_groups` overlap the principal's groups in a MATERIALIZED common table expression, and
only the outer query ranks and limits. Postgres must compute a materialized CTE on its own, so
no plan or index can rank a Chunk the principal may not see, and the top `limit` are always
the best allowed Chunks, never the best Chunks with the forbidden ones then removed. The
in-memory store keeps the same order of work. No groups, or no sources, finds nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Float, Select, bindparam, func, select

from cograil.domain import Chunk
from cograil.knowledge.embedding import cosine_similarity
from cograil.store_tables import EMBEDDING_DIMENSIONS
from cograil.store_tables import chunks as chunks_table


@dataclass(frozen=True)
class ScoredChunk:
    """A Chunk found by search; `score` is its cosine similarity to the query, at most 1."""

    chunk: Chunk
    score: float


def search_statement(
    vector: Sequence[float], groups: Sequence[str], sources: Sequence[str], limit: int
) -> Select[Any]:
    """Filter by groups and sources inside the CTE `allowed`; rank and limit outside it."""
    t = chunks_table
    allowed = (
        select(t.c.id, t.c.source, t.c.source_uri, t.c.text, t.c.acl_groups, t.c.embedding)
        .where(t.c.acl_groups.overlap(list(groups)))
        .where(t.c.source.in_(list(sources)))
        .where(t.c.embedding.is_not(None))
        .where(func.vector_norm(t.c.embedding) > 0)
        .cte("allowed")
        .prefix_with("MATERIALIZED")
    )
    query = bindparam("query", list(vector), type_=VECTOR(EMBEDDING_DIMENSIONS))
    distance = allowed.c.embedding.op("<=>", return_type=Float)(query).label("distance")
    columns = [allowed.c[name] for name in Chunk.model_fields]
    return select(*columns, distance).order_by(distance, allowed.c.id).limit(limit)


def rank(
    candidates: Iterable[tuple[Chunk, Sequence[float]]], vector: Sequence[float], limit: int
) -> list[ScoredChunk]:
    """The `limit` most similar of `candidates`, which are already filtered by groups."""
    scored = []
    for chunk, embedding in candidates:
        score = cosine_similarity(embedding, vector)
        if score is not None:
            scored.append(ScoredChunk(chunk.model_copy(deep=True), min(1.0, score)))
    scored.sort(key=lambda found: (-found.score, found.chunk.id))
    return scored[:limit]


def entitled(chunk: Chunk, groups: Iterable[str]) -> bool:
    """True when the Chunk names at least one of `groups`."""
    return not set(chunk.acl_groups).isdisjoint(groups)
