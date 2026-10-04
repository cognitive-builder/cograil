"""Find and read the documents of a KnowledgeSource folder: Markdown and PDF."""

from __future__ import annotations

import zlib
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from cograil.errors import KnowledgeSourceError

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
SUFFIXES = MARKDOWN_SUFFIXES | {".pdf"}
# pypdf raises more than PyPdfError on damaged files.
_UNREADABLE = (
    OSError,
    UnicodeDecodeError,
    PyPdfError,
    ValueError,
    KeyError,
    zlib.error,
    RecursionError,
)


def find_documents(folder: Path) -> list[Path]:
    """Every Markdown and PDF file under `folder`, in path order. A file that resolves outside
    the folder (a symlink) is refused rather than read."""
    root = folder.resolve()
    found = sorted(p for p in folder.rglob("*") if p.suffix.lower() in SUFFIXES and p.is_file())
    for path in found:
        if not path.resolve().is_relative_to(root):
            raise KnowledgeSourceError(f"{path} points outside {folder}")
    return found


def read_document(path: Path) -> str:
    """The text of one document, without NUL characters (Postgres text cannot hold them)."""
    try:
        if path.suffix.lower() == ".pdf":
            reader = PdfReader(path)
            text = "\n\n".join(page.extract_text() for page in reader.pages)
        else:
            text = path.read_text(encoding="utf-8")
    except _UNREADABLE as exc:
        raise KnowledgeSourceError(f"cannot read {path}: {exc}") from exc
    return text.replace("\x00", "")
