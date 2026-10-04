"""Sign-in (issue #19): the dev principal, the OIDC code flow with a signed session cookie,
and group claims mapped through Audiences. The identity provider is a fake on an httpx
transport, so authlib runs the real flow (discovery, code exchange, JWKS, ID token checks)."""

import time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from cograil.api.auth import SESSION_COOKIE, idp_http, install_auth
from cograil.api.auth_settings import auth_settings
from cograil.domain import Audience, Principal, Workspace
from cograil.errors import AuthNotConfigured

HTTP = idp_http()  # the package authlib calls the identity provider through
ISSUER = "https://idp.test"
CLIENT_ID = "cograil-client"
SECRET = "s" * 32
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
    "id_token_signing_alg_values_supported": ["RS256"],
}
OIDC_ENV = {
    "COGRAIL_AUTH": "oidc",
    "COGRAIL_OIDC_METADATA_URL": f"{ISSUER}/.well-known/openid-configuration",
    "COGRAIL_OIDC_CLIENT_ID": CLIENT_ID,
    "COGRAIL_OIDC_CLIENT_SECRET": "client-secret-value",
    "COGRAIL_OIDC_ALLOWED_DOMAINS": "example.com, Corp.Example.com",
    "COGRAIL_SESSION_SECRET": SECRET,
}
ENTRA_ENV = {**OIDC_ENV, "COGRAIL_OIDC_ID_CLAIM": "preferred_username"}
WORKSPACE = Workspace(
    name="w",
    colleagues=[],
    protocols=[],
    tools=[],
    audiences=[
        Audience(name="finance", groups=["finance"], claims=["5b2c-finance-guid"]),
        Audience(name="managers", groups=["managers"], claims=["managers@example.com"]),
    ],
    principals=[Principal(id="alice@example.com", aliases=["asmith@corp.example.com"],
                          groups=["all-employees"]),
                Principal(id="scheduler@example.com", kind="system")],
)  # fmt: skip
GOOGLE = {"email": "Bob@Example.com", "email_verified": True}
ENTRA = {"preferred_username": "ASmith@corp.example.com", "groups": ["5b2c-finance-guid", "x"]}


class FakeIdp:
    """Discovery, JWKS and a token endpoint that issues an ID token carrying `claims`."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, str]] = []

    def handle(self, request: Any) -> Any:
        if request.url.path == "/.well-known/openid-configuration":
            return HTTP.Response(200, json=METADATA)
        if request.url.path == "/jwks":
            return HTTP.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if request.url.path == "/token":
            self.token_requests.append(dict(parse_qsl(request.content.decode())))
            token = {"access_token": "at", "token_type": "Bearer", "id_token": self.id_token()}
            return HTTP.Response(200, json=token)
        return HTTP.Response(404)

    def id_token(self) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "1", "iat": now, "exp": now + 300}
        return jwt.encode({"alg": "RS256", "kid": "k1"}, {**claims, **self.claims}, self.key)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def client(env: dict[str, str], idp: FakeIdp | None = None) -> TestClient:
    app = FastAPI()
    transport = HTTP.MockTransport(idp.handle) if idp else None
    install_auth(app, auth_settings(env), WORKSPACE, transport=transport)
    return TestClient(app, base_url="https://testserver")


def sign_in(
    web: TestClient, idp: FakeIdp, claims: dict[str, Any], *, back_to: str = "", **query: str
) -> Any:
    """Follow /auth/login to the provider, then come back to the callback with a code.
    `back_to` is the `next` path the login was asked to return the browser to."""
    params = {"next": back_to} if back_to else None
    login = web.get("/auth/login", params=params, follow_redirects=False)
    sent = {k: v[0] for k, v in parse_qs(urlsplit(login.headers["location"]).query).items()}
    idp.claims = {"nonce": sent["nonce"], **claims}
    params = {"code": "the-code", "state": sent["state"], **query}
    return web.get("/auth/callback", params=params, follow_redirects=False)


def test_dev_mode_signs_everyone_in_as_the_configured_principal() -> None:
    env = {"COGRAIL_AUTH": "dev", "COGRAIL_DEV_PRINCIPAL": " Alice@Example.com",
           "COGRAIL_DEV_GROUPS": "finance, managers"}  # fmt: skip
    me = client(env).get("/auth/me").json()
    assert (me["id"], me["groups"]) == (
        "alice@example.com", ["all-employees", "finance", "managers"]
    )  # fmt: skip


def test_dev_mode_refuses_a_system_principal() -> None:
    env = {"COGRAIL_AUTH": "dev", "COGRAIL_DEV_PRINCIPAL": "scheduler@example.com"}
    with pytest.raises(AuthNotConfigured, match="is not a user"):
        client(env)


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({}, "COGRAIL_AUTH must be dev or oidc"),
        ({"COGRAIL_AUTH": "none"}, "COGRAIL_AUTH must be dev or oidc"),
        ({"COGRAIL_AUTH": "dev"}, "COGRAIL_DEV_PRINCIPAL"),
        ({**OIDC_ENV, "COGRAIL_OIDC_CLIENT_ID": ""}, "COGRAIL_OIDC_CLIENT_ID"),
        ({**OIDC_ENV, "COGRAIL_OIDC_ALLOWED_DOMAINS": " , "}, "COGRAIL_OIDC_ALLOWED_DOMAINS"),
        ({**OIDC_ENV, "COGRAIL_SESSION_SECRET": "short-secret"}, "COGRAIL_SESSION_SECRET"),
    ],
    ids=["unset", "unknown-mode", "dev-no-principal", "no-client", "no-domains", "short-secret"],
)
def test_missing_or_weak_settings_fail_closed(env: dict[str, str], named: str) -> None:
    with pytest.raises(AuthNotConfigured, match=named) as raised:
        auth_settings(env)
    assert "short-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("env", "claims", "expected"),
    [
        (OIDC_ENV, GOOGLE, ("bob@example.com", [])),
        (ENTRA_ENV, ENTRA, ("alice@example.com", ["all-employees", "finance"])),
    ],
    ids=["google", "entra-alias-and-groups"],
)
def test_oidc_code_flow_keeps_the_principal_in_a_signed_cookie(
    idp: FakeIdp, env: dict[str, str], claims: dict[str, Any], expected: tuple[str, list[str]]
) -> None:
    web = client(env, idp)
    assert web.get("/auth/me").status_code == 401
    login = web.get("/auth/login", follow_redirects=False)
    sent = parse_qs(urlsplit(login.headers["location"]).query)
    assert login.headers["location"].startswith(f"{ISSUER}/authorize?")
    assert (sent["response_type"], sent["client_id"]) == (["code"], [CLIENT_ID])
    assert sent["redirect_uri"] == ["https://testserver/auth/callback"]
    assert set(sent["scope"][0].split()) == {"openid", "email", "profile"}
    done = sign_in(web, idp, claims)
    assert (done.status_code, done.headers["location"]) == (303, "/")
    assert idp.token_requests[-1]["grant_type"] == "authorization_code"
    assert idp.token_requests[-1]["code"] == "the-code"
    cookie = done.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert {"httponly", "secure", "samesite=lax"} <= {p.strip() for p in cookie.split(";")}
    me = web.get("/auth/me").json()
    assert (me["id"], me["groups"]) == expected
    assert web.post("/auth/logout", follow_redirects=False).status_code == 303
    assert web.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    ("back_to", "lands_on"),
    [
        ("/?approval=gate-token", "/?approval=gate-token"),
        ("/", "/"),
        ("https://evil.test/phish", "/"),
        ("//evil.test/phish", "/"),
        ("/\\evil.test/phish", "/"),
        ("\\evil.test/phish", "/"),
        ("relative/path", "/"),
    ],
    ids=[
        "approval-card",
        "home",
        "other-origin",
        "protocol-relative",
        "slash-backslash",
        "backslash",
        "relative",
    ],
)
def test_sign_in_returns_to_a_same_site_path(idp: FakeIdp, back_to: str, lands_on: str) -> None:
    web = client(OIDC_ENV, idp)
    done = sign_in(web, idp, GOOGLE, back_to=back_to)
    assert (done.status_code, done.headers["location"]) == (303, lands_on)


def test_a_tampered_session_cookie_is_not_a_principal(idp: FakeIdp) -> None:
    web = client(OIDC_ENV, idp)
    sign_in(web, idp, GOOGLE)
    signed = web.cookies[SESSION_COOKIE]
    payload, _, signature = signed.partition(".")
    fresh = client(OIDC_ENV, idp)

    def me(value: str) -> int:
        return fresh.get("/auth/me", headers={"cookie": f"{SESSION_COOKIE}={value}"}).status_code

    assert me(signed) == 200
    assert me(f"{payload[:-2]}AA.{signature}") == me(payload) == 401


@pytest.mark.parametrize(
    ("claims", "query"),
    [
        ({"email": "eve@elsewhere.com", "email_verified": True}, {}),
        ({"email": "eve@example.com", "email_verified": False}, {}),
        ({"email": "eve@example.com"}, {}),
        ({"email": "Scheduler@example.com", "email_verified": True}, {}),
        ({**GOOGLE, "nonce": "replayed"}, {}),
        ({**GOOGLE, "aud": "another-client"}, {}),
        ({**GOOGLE, "iss": "https://evil.test"}, {}),
        (GOOGLE, {"state": "forged"}),
        (GOOGLE, {"error": "access_denied"}),
    ],
    ids=["domain", "unverified", "no-verified-claim", "system-principal", "nonce", "audience",
         "issuer", "state", "provider-error"],
)  # fmt: skip
def test_sign_in_is_refused_and_leaves_no_session(
    idp: FakeIdp, claims: dict[str, Any], query: dict[str, str]
) -> None:
    web = client(OIDC_ENV, idp)
    refused = sign_in(web, idp, claims, **query)
    assert refused.status_code == 403
    assert web.get("/auth/me").status_code == 401
