"""Split a document's text into overlapping chunks."""

from __future__ import annotations

from cograil.domain import MAX_CHUNK_CHARS
from cograil.errors import KnowledgeSourceError


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Windows of at most `size` characters; each starts `overlap` characters before the
    previous one ended. A window ends at whitespace when one lies in its second half, so words
    are not cut when they need not be. Blank text gives no chunks. `size` is at most
    MAX_CHUNK_CHARS."""
    if not 1 <= size <= MAX_CHUNK_CHARS or not 0 <= overlap < size:
        raise KnowledgeSourceError(
            f"need 0 <= overlap < size <= {MAX_CHUNK_CHARS}, got size={size} overlap={overlap}"
        )
    text = text.strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _window_end(text, start, size, overlap)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = end - overlap  # end > start + overlap, so every pass moves forward
    return chunks


def _window_end(text: str, start: int, size: int, overlap: int) -> int:
    end = min(start + size, len(text))
    if end == len(text):
        return end
    floor = start + max(overlap + 1, size // 2)
    for index in range(end, floor - 1, -1):
        if text[index - 1].isspace():
            return index
    return end
