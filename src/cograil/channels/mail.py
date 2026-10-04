"""Email delivery for approvals (issue #24): SMTP or Resend, chosen by environment variables.

- COGRAIL_EMAIL                 `smtp` or `resend`; unset means no approval emails
- COGRAIL_EMAIL_FROM            the sender address
- COGRAIL_SMTP_HOST, COGRAIL_SMTP_PORT (587), COGRAIL_SMTP_USER, COGRAIL_SMTP_PASSWORD,
  COGRAIL_SMTP_STARTTLS (true)  for `smtp`
- RESEND_API_KEY                for `resend`

Settings never come from Workspace files. A mode with a missing setting stops start-up with
EmailNotConfigured; a provider that refuses an email raises EmailDeliveryError.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from cograil.errors import EmailDeliveryError, EmailNotConfigured

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 20


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class SmtpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str
    host: str
    port: int = 587
    username: str | None = None
    password: SecretStr | None = None
    starttls: bool = True


class ResendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str
    api_key: SecretStr


type EmailSettings = SmtpSettings | ResendSettings


def email_settings(env: Mapping[str, str]) -> EmailSettings | None:
    """The mail settings in `env`; None when COGRAIL_EMAIL is unset."""
    mode = env.get("COGRAIL_EMAIL", "").strip().lower()
    if not mode:
        return None
    sender = _need(env, "COGRAIL_EMAIL_FROM")
    if mode == "resend":
        return ResendSettings(sender=sender, api_key=SecretStr(_need(env, "RESEND_API_KEY")))
    if mode != "smtp":
        raise EmailNotConfigured(f"COGRAIL_EMAIL={mode!r} is not smtp or resend")
    password = env.get("COGRAIL_SMTP_PASSWORD")
    try:
        port = int(env.get("COGRAIL_SMTP_PORT", "587"))
    except ValueError:
        raise EmailNotConfigured("COGRAIL_SMTP_PORT is not a number") from None
    return SmtpSettings(
        sender=sender,
        host=_need(env, "COGRAIL_SMTP_HOST"),
        port=port,
        username=env.get("COGRAIL_SMTP_USER") or None,
        password=SecretStr(password) if password else None,
        starttls=env.get("COGRAIL_SMTP_STARTTLS", "true").strip().lower() != "false",
    )


def _need(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise EmailNotConfigured(f"{name} is not set")
    return value


def build_sender(
    settings: EmailSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> EmailSender:
    """The sender for `settings`; `transport` carries Resend's HTTP calls (tests only)."""
    if isinstance(settings, SmtpSettings):
        return SmtpSender(settings)
    return ResendSender(settings, transport=transport)


class SmtpSender:
    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    async def send(self, message: EmailMessage) -> None:
        try:
            await asyncio.to_thread(self._send, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError(f"smtp: {type(exc).__name__}") from exc

    def _send(self, message: EmailMessage) -> None:
        s = self._settings
        with smtplib.SMTP(s.host, s.port, timeout=TIMEOUT_SECONDS) as client:
            if s.starttls:
                client.starttls(context=ssl.create_default_context())
            if s.username and s.password:
                client.login(s.username, s.password.get_secret_value())
            client.send_message(message)


class ResendSender:
    def __init__(
        self, settings: ResendSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def send(self, message: EmailMessage) -> None:
        body: dict[str, Any] = {
            "from": str(message["From"]),
            "to": [str(message["To"])],
            "subject": str(message["Subject"]),
            "text": message.get_content(),
        }
        headers = {"Authorization": f"Bearer {self._settings.api_key.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as http:
                response = await http.post(RESEND_URL, json=body, headers=headers)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"resend: {type(exc).__name__}") from exc
