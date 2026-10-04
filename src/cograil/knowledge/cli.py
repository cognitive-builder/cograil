"""`cograil knowledge`: sync a workspace's KnowledgeSources into the store."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from cograil.errors import KnowledgeSourceError, WorkspaceError
from cograil.knowledge.store import KnowledgeStore, PostgresKnowledgeStore
from cograil.knowledge.sync import sync_workspace
from cograil.workspace import load_workspace

knowledge_app = typer.Typer(help="Load a workspace's knowledge folders.", no_args_is_help=True)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@asynccontextmanager
async def open_knowledge_store() -> AsyncIterator[KnowledgeStore]:
    """The Postgres KnowledgeStore named by DATABASE_URL (postgresql+asyncpg://...)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        _fail("DATABASE_URL is not set")
    store = PostgresKnowledgeStore.from_url(url)
    try:
        yield store
    finally:
        await store.dispose()


@knowledge_app.command()
def sync(
    workspace: Annotated[Path, typer.Argument(help="Workspace folder.")],
) -> None:
    """Read each KnowledgeSource folder and make the store hold its Chunks.

    Markdown and PDF files are cut into Chunks of the source's chunk_size characters with
    chunk_overlap characters shared between neighbours. Each Chunk carries the acl_groups of
    its file: `<file>.acl.yaml` beside it, else the nearest `.acl.yaml` in its folders, else
    the source's own acl_groups. Idempotent: syncing unchanged files again changes nothing, and
    Chunks of files that were edited or deleted are rewritten or removed.

    \b
    Exit codes: 0 synced; 1 error (invalid workspace, unreadable folder, document or ACL
    file, missing DATABASE_URL); 2 usage error.
    """
    asyncio.run(_sync(workspace))


async def _sync(path: Path) -> None:
    try:
        loaded = load_workspace(path)
    except WorkspaceError as exc:
        _fail(f"invalid: {exc}")
    async with open_knowledge_store() as store:
        try:
            reports = await sync_workspace(path, loaded, store)
        except KnowledgeSourceError as exc:
            _fail(f"invalid: {exc}")
    for each in reports:
        c = each.counts
        typer.echo(
            f"{each.source}: {each.files} files, {each.chunks} chunks "
            f"(added {c.added}, updated {c.updated}, removed {c.removed}, unchanged {c.unchanged})"
        )
    if not reports:
        typer.echo(f"{loaded.name}: no knowledge sources")
