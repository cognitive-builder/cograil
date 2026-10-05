"""Signed approval links: the one-click address an approver gets by email (issue #24).

A link names one Approval and carries its expiry and an HMAC-SHA256 signature over the token,
the approver and the expiry, keyed by COGRAIL_APPROVAL_LINK_SECRET. Whoever holds the link is
the approver for that one Approval: `verify` accepts no other token, approver or expiry. The
link expires with its Approval (`approvals.timeout_hours`) and is single-use because an
Approval is decided once: after a decision the link only answers that it was decided.

A link is a working credential until then, so `hide_link_queries` keeps its signature out of the
web server's access log (docs/threat-model.md).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
from datetime import UTC, datetime
from urllib.parse import urlencode

from cograil.domain import Approval
from cograil.errors import ApprovalLinkInvalid
from cograil.identity import normalise_principal_id

MIN_SECRET_CHARS = 32
LINK_PATH = "/approvals/link"
ACCESS_LOGGER = "uvicorn.access"
HIDDEN_QUERY = "?[REDACTED:link]"


class ApprovalLinks:
    def __init__(self, secret: str, base_url: str) -> None:
        if len(secret) < MIN_SECRET_CHARS:
            raise ValueError(f"the link secret needs at least {MIN_SECRET_CHARS} characters")
        self._key = secret.encode()
        self._base = base_url.rstrip("/")

    def link(self, approval: Approval) -> str:
        """The signed link for a pending Approval; one without an expiry gets none."""
        if approval.expires_at is None:
            raise ApprovalLinkInvalid(f"approval {approval.token} has no expiry to sign")
        # Rounded up, so a link is never expired while its Approval is still open.
        exp = math.ceil(approval.expires_at.timestamp())
        query = urlencode({"exp": exp, "sig": self._sign(approval.token, approval.approver, exp)})
        return f"{self._base}{LINK_PATH}/{approval.token}?{query}"

    def verify(self, approval: Approval, exp: int, sig: str) -> None:
        """Raise ApprovalLinkInvalid unless `sig` signs this Approval's token, approver and exp."""
        expected = self._sign(approval.token, approval.approver, exp)
        if not hmac.compare_digest(expected.encode(), sig.encode()):  # bytes: any sig is safe
            raise ApprovalLinkInvalid("this link is not valid")

    @staticmethod
    def expired(exp: int, now: datetime) -> bool:
        return now >= datetime.fromtimestamp(exp, UTC)

    def _sign(self, token: str, approver: str, exp: int) -> str:
        payload = f"{token}\n{normalise_principal_id(approver)}\n{exp}".encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


class LinkQueryFilter(logging.Filter):
    """Replaces the query (`exp` and `sig`) of an approval link in a log record's arguments,
    where uvicorn puts the request path; the record is kept."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_without_link_query(arg) for arg in record.args)
        return True


def _without_link_query(value: object) -> object:
    if isinstance(value, str) and f"{LINK_PATH}/" in value and "?" in value:
        return value.partition("?")[0] + HIDDEN_QUERY
    return value


def hide_link_queries(logger_name: str = ACCESS_LOGGER) -> None:
    """Install LinkQueryFilter on the access logger, once however often the app is built."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, LinkQueryFilter) for f in logger.filters):
        logger.addFilter(LinkQueryFilter())
