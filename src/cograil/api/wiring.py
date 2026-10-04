"""Start the service from the environment: `uvicorn --factory cograil.api.wiring:app_from_env`.

- COGRAIL_WORKSPACE  the workspace folder (default: the current directory)
- DATABASE_URL       the Postgres RunStore, a SQLAlchemy URL (postgresql+asyncpg://...)
- ANTHROPIC_API_KEY  read by the Anthropic provider
- COGRAIL_AUTH and the settings of its mode: see cograil.api.auth_settings and docs/auth.md
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from cograil.api.app import create_app
from cograil.api.auth_settings import auth_settings
from cograil.domain import Workspace
from cograil.errors import StoreNotConfigured
from cograil.knowledge.store import PostgresKnowledgeStore
from cograil.knowledge.tool import add_knowledge
from cograil.orchestrator import classification_model
from cograil.providers import AnthropicProvider
from cograil.redaction import Redactor, small_tier_redactor
from cograil.registry import ToolRegistry, build_registry
from cograil.store import PostgresRunStore, RunStore
from cograil.workspace import load_workspace


async def open_registry(
    workspace: Workspace, store: RunStore, root: Path, redactor: Redactor | None = None
) -> ToolRegistry:
    """The Tools of the workspace; knowledge Tools search the Chunks in DATABASE_URL."""
    registry = await build_registry(workspace, store, root, redactor=redactor)
    url = os.environ.get("DATABASE_URL")
    if url and any(tool.kind == "knowledge" for tool in workspace.tools):
        knowledge = PostgresKnowledgeStore.from_url(url)
        registry.on_close(knowledge.dispose)
        add_knowledge(registry, workspace, knowledge)
    return registry


def app_from_env() -> FastAPI:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise StoreNotConfigured("DATABASE_URL is not set")
    path = Path(os.environ.get("COGRAIL_WORKSPACE", "."))
    workspace = load_workspace(path)
    store = PostgresRunStore.from_url(url)
    redactor = small_tier_redactor(workspace)  # ADR 0010

    async def open_with_redaction(
        workspace: Workspace, store: RunStore, root: Path
    ) -> ToolRegistry:
        return await open_registry(workspace, store, root, redactor)

    return create_app(
        workspace,
        path,
        store,
        auth=auth_settings(os.environ),
        classifier=AnthropicProvider(classification_model(workspace)),
        provider_for=AnthropicProvider.for_protocol,
        open_registry=open_with_redaction,
        close=store.dispose,
        redactor=redactor,
    )
