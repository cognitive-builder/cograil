"""Embedder: turns text into the vectors that knowledge search ranks Chunks by.

The default, HashingEmbedder, needs no model and no network: it hashes each lower-cased word
into one of EMBEDDING_DIMENSIONS buckets and scales the counts to unit length, so cosine
similarity rewards shared words. It is deterministic, which keeps sync idempotent and tests
exact. A model-backed Embedder can replace it behind the same Protocol; its vectors must have
the width of the `chunks.embedding` column.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from cograil.store_tables import EMBEDDING_DIMENSIONS

_WORD = re.compile(r"\w+")


@runtime_checkable
class Embedder(Protocol):
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of `dimensions` floats per text, in order. A text without words may give
        the zero vector, which matches nothing."""
        ...


class HashingEmbedder:
    """Feature hashing of words: local, deterministic and free."""

    dimensions = EMBEDDING_DIMENSIONS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_hashed(text, self.dimensions) for text in texts]


def _hashed(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for word in _WORD.findall(text.lower()):
        digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
        vector[int.from_bytes(digest) % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Cosine of the angle between `a` and `b`; None when either is the zero vector."""
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if not norms:
        return None
    return sum(x * y for x, y in zip(a, b, strict=True)) / norms
