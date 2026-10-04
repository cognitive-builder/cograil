"""Sign-in for the web service: every request becomes a Principal (cograil.api.auth_settings).

`install_auth(app, settings, workspace)` sets the mode up once; routers then take the signed-in
Principal with `Depends(current_principal)`, which answers 401 when nobody is signed in.

- dev: every request is the configured dev principal, resolved through the workspace like
  anyone else, plus COGRAIL_DEV_GROUPS. A warning is logged at start-up.
- oidc: `/auth/login` sends the browser to the identity provider (authorization code flow,
  with state and nonce, via authlib); `/auth/callback` exchanges the code, validates the ID
  token (signature, issuer, audience, nonce, expiry) and keeps the Principal in a session
  cookie signed with COGRAIL_SESSION_SECRET (HttpOnly, Secure, SameSite=Lax, 8 hours);
  `POST /auth/logout` clears it. A `/auth/login?next=<path>` holds a same-site path the
  callback returns the browser to after sign-in, so a link like `/?approval=<token>` survives
  it; any other `next` falls back to `/`. A sign-in is refused unless the id claim holds an email in
  one of COGRAIL_OIDC_ALLOWED_DOMAINS, verified by the provider when the claim is `email`.
- both: `GET /auth/me` returns the signed-in Principal.

The Principal's groups are its workspace groups plus those of every Audience whose `claims`
list one of the values in the groups claim (cograil.audience.resolve_principal). The
groups are fixed at sign-in until the session ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from cograil.api.auth_settings import AuthSettings, DevAuth, OidcAuth
from cograil.audience import resolve_principal
from cograil.domain import Principal, Workspace
from cograil.errors import AuthNotConfigured, LoginDenied
from cograil.identity import normalise_principal_id
from cograil.observability import log_event

SESSION_COOKIE = "cograil_session"
SESSION_MAX_AGE = 8 * 60 * 60
SESSION_KEY = "principal"
NEXT_KEY = "next"  # where /auth/login was asked to bring the browser back to
SCOPES = "openid email profile"

type PrincipalResolver = Callable[[Request], Principal]


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: the signed-in Principal; 401 when there is none."""
    resolver: PrincipalResolver | None = getattr(request.app.state, "cograil_principal", None)
    if resolver is None:
        raise AuthNotConfigured("install_auth was not called for this app")
    return resolver(request)


def install_auth(
    app: FastAPI,
    settings: AuthSettings,
    workspace: Workspace,
    *,
    transport: Any = None,
) -> None:
    """Set sign-in up on app. `transport`, an `idp_http()` transport, carries the identity
    provider calls (tests only)."""
    if isinstance(settings, DevAuth):
        principal = resolve_principal(workspace, settings.principal, groups=settings.groups)
        if principal.kind != "user":
            raise AuthNotConfigured(f"COGRAIL_DEV_PRINCIPAL {principal.id} is not a user")
        app.state.cograil_principal = lambda _request: principal
        log_event("auth.dev_mode", logging.WARNING, principal_id=principal.id)
    else:
        from starlette.middleware.sessions import SessionMiddleware

        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret.get_secret_value(),
            session_cookie=SESSION_COOKIE,
            max_age=SESSION_MAX_AGE,
            same_site="lax",
            https_only=True,
        )
        app.include_router(_oidc_router(settings, workspace, _oidc_client(settings, transport)))
        app.state.cograil_principal = _session_principal
    app.include_router(_me_router())


def principal_from_claims(
    settings: OidcAuth, workspace: Workspace, claims: Mapping[str, Any]
) -> Principal:
    """The Principal validated ID token claims sign in as; raise LoginDenied if none may."""
    raw = claims.get(settings.id_claim)
    if not isinstance(raw, str) or "@" not in raw:
        raise LoginDenied(f"the {settings.id_claim} claim does not hold an email")
    if settings.id_claim == "email" and claims.get("email_verified") is not True:
        raise LoginDenied("the identity provider has not verified the email")
    principal_id = normalise_principal_id(raw)
    domain = principal_id.rpartition("@")[2]
    if domain not in settings.allowed_domains:
        raise LoginDenied(f"{domain} is not an allowed domain")
    overage = claims.get("_claim_names")
    if isinstance(overage, dict) and settings.groups_claim in overage:
        log_event("auth.groups_overage", logging.WARNING, principal_id=principal_id)
    values = _claim_values(claims.get(settings.groups_claim))
    principal = resolve_principal(workspace, principal_id, claims=values)
    if principal.kind != "user":
        raise LoginDenied(f"{principal.id} is a {principal.kind} principal; people only")
    return principal


def _claim_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _session_principal(request: Request) -> Principal:
    try:
        return Principal.model_validate(request.session[SESSION_KEY])
    except (KeyError, ValidationError):
        raise HTTPException(401, "sign in at /auth/login") from None


def _same_site_path(value: object) -> str:
    """A path back into this service only: absolute, and never another origin."""
    if isinstance(value, str) and value.startswith("/") and not value.startswith(("//", "\\")):
        return value
    return "/"


def idp_http() -> Any:
    """The HTTP package authlib reaches the identity provider with: httpx2, else httpx."""
    from authlib.integrations.httpx_client._compat import (  # type: ignore[import-untyped]
        httpx2,
    )

    return httpx2


def _oidc_client(settings: OidcAuth, transport: Any) -> Any:
    from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]

    client_kwargs: dict[str, Any] = {"scope": SCOPES}
    if transport is not None:
        client_kwargs["transport"] = transport
    return OAuth().register(
        "idp",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        server_metadata_url=settings.metadata_url,
        client_kwargs=client_kwargs,
    )


def _oidc_router(settings: OidcAuth, workspace: Workspace, client: Any) -> APIRouter:
    router = APIRouter(prefix="/auth")

    @router.get("/login")
    async def login(request: Request) -> Response:
        request.session[NEXT_KEY] = _same_site_path(request.query_params.get(NEXT_KEY))
        redirect_uri = settings.redirect_url or str(request.url_for("auth_callback"))
        response: Response = await client.authorize_redirect(request, redirect_uri)
        return response

    @router.get("/callback", name="auth_callback")
    async def callback(request: Request) -> Response:
        principal = await _sign_in(settings, workspace, client, request)
        back = _same_site_path(request.session.get(NEXT_KEY))
        request.session.clear()
        request.session[SESSION_KEY] = principal.model_dump(mode="json")
        log_event("auth.signed_in", principal_id=principal.id, groups=principal.groups)
        return RedirectResponse(back, status_code=303)

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    return router


async def _sign_in(
    settings: OidcAuth, workspace: Workspace, client: Any, request: Request
) -> Principal:
    """Finish the code flow; any failure, from the provider or the claims, answers 403."""
    from authlib.common.errors import AuthlibBaseError  # type: ignore[import-untyped]
    from joserfc.errors import JoseError

    try:
        token = await client.authorize_access_token(request)
        if "userinfo" not in token:
            raise LoginDenied("the identity provider returned no ID token")
        return principal_from_claims(settings, workspace, token["userinfo"])
    except (AuthlibBaseError, JoseError, LoginDenied, idp_http().HTTPError) as exc:
        request.session.clear()
        log_event("auth.denied", logging.WARNING, reason=f"{type(exc).__name__}: {exc}")
        raise HTTPException(403, "sign-in refused") from None


def _me_router() -> APIRouter:
    router = APIRouter(prefix="/auth")

    @router.get("/me")
    async def me(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        return principal

    return router
