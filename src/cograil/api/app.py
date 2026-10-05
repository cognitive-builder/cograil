"""The web service: FastAPI app over a workspace, a RunStore and the sign-in of cograil.api.auth.

Routes: POST /chat (SSE), GET /runs, GET /runs/{id}, GET and POST /approvals/{token}, GET /audit,
GET /health, POST /slack/events (when Slack is set up), the sign-in routes of cograil.api.auth,
the web pages at GET / (the chat) and GET /history (the run history), and the OpenAPI docs at
/docs. Everything but /health, the web pages, the sign-in routes, POST /slack/events (Slack's own
signature) and the signed email links of GET and POST /approvals/link/{token}
needs a signed-in Principal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI

from cograil import __version__
from cograil.api import approvals, audit, chat, link_decisions, runs
from cograil.api.approval_mail import ApprovalMail
from cograil.api.auth import install_auth
from cograil.api.auth_settings import AuthSettings
from cograil.api.services import ProviderFactory, RegistryOpener, Services
from cograil.channels import web
from cograil.channels.slack import SlackSettings
from cograil.domain import Workspace
from cograil.errors import SlackNotConfigured
from cograil.providers.base import Provider
from cograil.redaction import Redactor
from cograil.scheduler import build_scheduler, schedules
from cograil.store import RunStore


def _slack_router(settings: SlackSettings, services: Services) -> APIRouter:
    try:
        from cograil.channels.slack.bolt import slack_router
    except ImportError as exc:  # the optional `slack` extra
        raise SlackNotConfigured("SLACK_BOT_TOKEN is set; install the `slack` extra") from exc
    return slack_router(settings, services)


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
    redactor: Redactor | None = None,
    approval_mail: ApprovalMail | None = None,
    slack: SlackSettings | None = None,
) -> FastAPI:
    """The service for the workspace loaded from `path`.

    `classifier` routes chat messages on the small tier, `provider_for` gives the Provider a
    Protocol's Runs use, `redactor` redacts the opt-in message snippet log, `approval_mail`
    emails approvers a signed link (without it the link routes answer 404), `slack` turns on
    POST /slack/events (cograil.channels.slack), and `close` runs at shutdown. The Colleagues'
    Schedules run while the app is up (cograil.scheduler).
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = build_scheduler(workspace, services.scheduled) if schedules(workspace) else None
        if scheduler is not None:
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        # A Run is a detached task; let the ones in flight finish before the store goes.
        await asyncio.gather(*services.tasks, return_exceptions=True)
        if close is not None:
            await close()

    services = Services(
        workspace, path, store,
        classifier=classifier, provider_for=provider_for, open_registry=open_registry,
        redactor=redactor, approval_mail=approval_mail,
    )  # fmt: skip
    app = FastAPI(title="Cograil", version=__version__, lifespan=lifespan)
    app.state.cograil_services = services
    install_auth(app, auth, workspace)
    routers = (
        web.router,
        chat.router,
        runs.router,
        approvals.router,
        link_decisions.router,
        audit.router,
    )
    for router in routers:
        app.include_router(router)
    if slack is not None:
        app.include_router(_slack_router(slack, services))

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness: the service is up. No sign-in needed."""
        return {"status": "ok"}

    return app
