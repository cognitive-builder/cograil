"""Sign-in settings, read from the environment (secrets never come from workspace files).

COGRAIL_AUTH must be set; nothing defaults to a mode, so a deployment that forgot it fails to
start instead of letting everyone in.

- `dev`: every request is COGRAIL_DEV_PRINCIPAL with the groups in COGRAIL_DEV_GROUPS
  (comma-separated). For local work and demos only: refused when COGRAIL_ENV=production.
- `oidc`: OpenID Connect authorization code flow (authlib). Needs COGRAIL_OIDC_METADATA_URL,
  COGRAIL_OIDC_CLIENT_ID, COGRAIL_OIDC_CLIENT_SECRET, COGRAIL_OIDC_ALLOWED_DOMAINS
  (comma-separated email domains allowed to sign in) and COGRAIL_SESSION_SECRET (at least
  32 characters; it signs the session cookie). Optional: COGRAIL_OIDC_ID_CLAIM (the claim
  that names the principal, default `email`), COGRAIL_OIDC_GROUPS_CLAIM (default `groups`)
  and COGRAIL_OIDC_REDIRECT_URL (the callback URL, when a proxy hides the public one).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

from cograil.errors import AuthNotConfigured
from cograil.identity import PrincipalId

MIN_SECRET_LENGTH = 32


class DevAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dev"] = "dev"
    principal: PrincipalId
    groups: list[str] = []

    @field_validator("principal")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value:
            raise ValueError("must name a principal")
        return value


class OidcAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["oidc"] = "oidc"
    metadata_url: str
    client_id: str
    client_secret: SecretStr
    session_secret: SecretStr
    allowed_domains: list[str]
    id_claim: str = "email"
    groups_claim: str = "groups"
    redirect_url: str | None = None

    @field_validator("session_secret")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def _some_domains(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must name at least one email domain")
        return [domain.lower() for domain in value]


type AuthSettings = DevAuth | OidcAuth

_OIDC_ENV = {
    "metadata_url": "COGRAIL_OIDC_METADATA_URL",
    "client_id": "COGRAIL_OIDC_CLIENT_ID",
    "client_secret": "COGRAIL_OIDC_CLIENT_SECRET",
    "session_secret": "COGRAIL_SESSION_SECRET",
    "id_claim": "COGRAIL_OIDC_ID_CLAIM",
    "groups_claim": "COGRAIL_OIDC_GROUPS_CLAIM",
    "redirect_url": "COGRAIL_OIDC_REDIRECT_URL",
}


def auth_settings(env: Mapping[str, str]) -> AuthSettings:
    """The sign-in settings in env; raise AuthNotConfigured naming what is missing or wrong."""
    mode = env.get("COGRAIL_AUTH", "").strip()
    try:
        if mode == "dev":
            if env.get("COGRAIL_ENV", "").strip().lower() == "production":
                raise AuthNotConfigured("COGRAIL_AUTH=dev is refused when COGRAIL_ENV=production")
            principal = env.get("COGRAIL_DEV_PRINCIPAL", "")
            return DevAuth(principal=principal, groups=_items(env.get("COGRAIL_DEV_GROUPS")))
        if mode == "oidc":
            fields = {key: env[name] for key, name in _OIDC_ENV.items() if env.get(name)}
            domains = _items(env.get("COGRAIL_OIDC_ALLOWED_DOMAINS"))
            return OidcAuth.model_validate({**fields, "allowed_domains": domains})
    except ValidationError as exc:
        raise AuthNotConfigured(f"COGRAIL_AUTH={mode}: {_problems(exc)}") from None
    raise AuthNotConfigured(f"COGRAIL_AUTH must be dev or oidc, not {mode!r}")


def _items(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _problems(exc: ValidationError) -> str:
    """Name the setting of each problem without echoing any value (secrets stay out of logs)."""
    names = {**_OIDC_ENV, "allowed_domains": "COGRAIL_OIDC_ALLOWED_DOMAINS",
             "principal": "COGRAIL_DEV_PRINCIPAL"}  # fmt: skip
    found = []
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else ""
        found.append(f"{names.get(field, field)}: {error['msg']}")
    return "; ".join(found)
