"""Signed approval links and the mail settings (issue #24)."""

import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cograil.approval_links import ApprovalLinks, LinkQueryFilter, hide_link_queries
from cograil.channels.mail import (
    ResendSender,
    ResendSettings,
    SmtpSettings,
    email_settings,
)
from cograil.domain import Approval
from cograil.errors import ApprovalLinkInvalid, EmailDeliveryError, EmailNotConfigured

SECRET = "s" * 32
T0 = datetime(2026, 10, 4, tzinfo=UTC)


def approval(**changes: object) -> Approval:
    fields: dict[str, object] = {
        "token": "ab" * 16, "run_id": "r1", "step": 2, "tool": "demo.record",
        "args": {"item": "42"}, "approver": "manager@example.com",
        "expires_at": T0 + timedelta(hours=72),
    }  # fmt: skip
    return Approval(**{**fields, **changes})  # type: ignore[arg-type]


def parts(link: str) -> tuple[str, int, str]:
    url = urlparse(link)
    query = parse_qs(url.query)
    return url.path, int(query["exp"][0]), query["sig"][0]


def test_a_link_names_its_approval_and_verifies() -> None:
    links = ApprovalLinks(SECRET, "https://cograil.example.com/")
    link = links.link(approval())
    path, exp, sig = parts(link)
    assert link.startswith("https://cograil.example.com/approvals/link/")
    assert path.endswith("ab" * 16)
    links.verify(approval(), exp, sig)


@pytest.mark.parametrize(
    "forged",
    [
        {"token": "cd" * 16},  # another Approval
        {"approver": "mallory@example.com"},  # another approver
    ],
)
def test_a_link_verifies_for_no_other_token_or_approver(forged: dict[str, str]) -> None:
    links = ApprovalLinks(SECRET, "https://x.example")
    _, exp, sig = parts(links.link(approval()))
    with pytest.raises(ApprovalLinkInvalid):
        links.verify(approval(**forged), exp, sig)


def test_a_link_refuses_a_moved_expiry_and_another_secret() -> None:
    links = ApprovalLinks(SECRET, "https://x.example")
    _, exp, sig = parts(links.link(approval()))
    with pytest.raises(ApprovalLinkInvalid):
        links.verify(approval(), exp + 3600, sig)
    with pytest.raises(ApprovalLinkInvalid):
        ApprovalLinks("t" * 32, "https://x.example").verify(approval(), exp, sig)


def test_a_link_expires_with_its_approval_and_a_weak_secret_is_refused() -> None:
    assert not ApprovalLinks.expired(1_000, datetime.fromtimestamp(999, UTC))
    assert ApprovalLinks.expired(1_000, datetime.fromtimestamp(1_000, UTC))
    with pytest.raises(ValueError):
        ApprovalLinks("short", "https://x.example")
    with pytest.raises(ApprovalLinkInvalid):
        ApprovalLinks(SECRET, "https://x.example").link(approval(expires_at=None))


def test_mail_settings_from_the_environment() -> None:
    assert email_settings({}) is None
    smtp = email_settings(
        {"COGRAIL_EMAIL": "smtp", "COGRAIL_EMAIL_FROM": "a@x.io", "COGRAIL_SMTP_HOST": "mail.x.io"}
    )
    assert isinstance(smtp, SmtpSettings) and (smtp.port, smtp.starttls) == (587, True)
    resend = email_settings(
        {"COGRAIL_EMAIL": "resend", "COGRAIL_EMAIL_FROM": "a@x.io", "RESEND_API_KEY": "k"}
    )
    assert isinstance(resend, ResendSettings)


@pytest.mark.parametrize(
    "env",
    [
        {"COGRAIL_EMAIL": "pigeon", "COGRAIL_EMAIL_FROM": "a@x.io"},
        {"COGRAIL_EMAIL": "smtp", "COGRAIL_EMAIL_FROM": "a@x.io"},
        {"COGRAIL_EMAIL": "resend", "COGRAIL_EMAIL_FROM": "a@x.io"},
        {"COGRAIL_EMAIL": "resend", "RESEND_API_KEY": "k"},
    ],
)
def test_incomplete_mail_settings_stop_start_up(env: dict[str, str]) -> None:
    with pytest.raises(EmailNotConfigured):
        email_settings(env)


def message() -> EmailMessage:
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "a@x.io", "b@x.io", "Hello"
    mail.set_content("body")
    return mail


async def test_resend_posts_the_email_and_refusal_is_a_typed_error() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200 if len(seen) == 1 else 422, json={})

    sender = ResendSender(
        ResendSettings(sender="a@x.io", api_key="k"),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    await sender.send(message())
    assert seen[0].headers["authorization"] == "Bearer k"
    assert b'"to":["b@x.io"]' in seen[0].content.replace(b" ", b"")
    with pytest.raises(EmailDeliveryError):
        await sender.send(message())


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/approvals/link/abab?exp=1&sig=c0ffee", "/approvals/link/abab?[REDACTED:link]"),
        ("/root/approvals/link/abab?exp=1&sig=c0ffee", "/root/approvals/link/abab?[REDACTED:link]"),
        ("/runs?limit=5", "/runs?limit=5"),  # other queries are left alone
    ],
)
def test_the_access_log_never_holds_a_links_signature(
    caplog: pytest.LogCaptureFixture, path: str, logged: str
) -> None:
    """A logged link would approve for whoever reads the log, until its Approval is decided."""
    name = "test.access"
    hide_link_queries(name)
    hide_link_queries(name)  # every app built in a process installs it; one filter is enough
    access = logging.getLogger(name)
    assert sum(isinstance(f, LinkQueryFilter) for f in access.filters) == 1
    with caplog.at_level(logging.INFO, logger=name):  # uvicorn's own access-log format
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", "GET", path, "1.1", 200)
    (line,) = caplog.messages
    assert f"GET {logged} HTTP/1.1" in line and "c0ffee" not in line
