"""The web service: FastAPI app over a workspace, a RunStore and the sign-in of cograil.api.auth.

Routes: POST /chat (SSE), GET /runs, GET /runs/{id}, GET and POST /approvals/{token}, GET /audit,
GET /health, the sign-in routes of cograil.api.auth, the web chat page at GET / and the OpenAPI
docs at /docs. Everything but /health, the web chat page and the sign-in routes needs a signed-in
Principal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from cograil import __version__
from cograil.api import approvals, audit, chat, runs
from cograil.api.auth import install_auth
from cograil.api.auth_settings import AuthSettings
from cograil.api.services import ProviderFactory, RegistryOpener, Services
from cograil.channels import web
from cograil.domain import Workspace
from cograil.providers.base import Provider
from cograil.store import RunStore


def create_app(
    workspace: Workspace,
    path: Path,
    store: RunStore,
    *,
    auth: AuthSettings,
    classifier: Provider,
    provider_for: ProviderFactory,
    open_registry: RegistryOpener,
    close: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """The service for the workspace loaded from `path`.

    `classifier` routes chat messages on the small tier, `provider_for` gives the Provider a
    Protocol's Runs use, and `close` runs at shutdown.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # A Run is a detached task; let the ones in flight finish before the store goes.
        await asyncio.gather(*services.tasks, return_exceptions=True)
        if close is not None:
            await close()

    services = Services(
        workspace, path, store,
        classifier=classifier, provider_for=provider_for, open_registry=open_registry,
    )  # fmt: skip
    app = FastAPI(title="Cograil", version=__version__, lifespan=lifespan)
    app.state.cograil_services = services
    install_auth(app, auth, workspace)
    for router in (web.router, chat.router, runs.router, approvals.router, audit.router):
        app.include_router(router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness: the service is up. No sign-in needed."""
        return {"status": "ok"}

    return app
