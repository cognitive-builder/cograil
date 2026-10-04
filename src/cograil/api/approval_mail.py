"""Tell an approver by email that a gate waits on them, with a signed link (issue #24).

Sent once per pending Approval after a Run stops at a gate. Each outcome is an AuditEvent of
the Run, `approval.emailed` or `approval.email_failed`, naming the approver and the link's
expiry, never the link itself (it is a credential). A failed email never fails the Run: the
approver can still open the approval card in the web chat. Tool arguments in the email are
data the Run planned, shown to the approver, never instructions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import EmailMessage

from cograil.approval_links import ApprovalLinks
from cograil.channels.mail import EmailSender, build_sender, email_settings
from cograil.domain import Approval, AuditEvent, Run
from cograil.errors import ApprovalLinkInvalid, EmailDeliveryError, EmailNotConfigured
from cograil.observability import log_event
from cograil.store import RunStore

MAX_ARGS_CHARS = 1000


class ApprovalMail:
    def __init__(self, sender: EmailSender, sender_address: str, links: ApprovalLinks) -> None:
        self._sender = sender
        self._from = sender_address
        self.links = links

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ApprovalMail | None:
        """None when COGRAIL_EMAIL is unset; otherwise needs COGRAIL_PUBLIC_URL (where the
        link points) and COGRAIL_APPROVAL_LINK_SECRET (32 characters or more)."""
        settings = email_settings(env)
        if settings is None:
            return None
        base = env.get("COGRAIL_PUBLIC_URL", "").strip()
        if not base.startswith(("https://", "http://")):
            raise EmailNotConfigured("COGRAIL_PUBLIC_URL is not an http(s) URL")
        try:
            links = ApprovalLinks(env.get("COGRAIL_APPROVAL_LINK_SECRET", ""), base)
        except ValueError as exc:
            raise EmailNotConfigured(f"COGRAIL_APPROVAL_LINK_SECRET: {exc}") from None
        return cls(build_sender(settings), settings.sender, links)

    async def notify(self, store: RunStore, run: Run) -> None:
        """Email the approver of each pending Approval of `run` not emailed yet."""
        emailed = {
            e.detail.get("token")
            for e in await store.list_audit_events(run.id)
            if e.kind == "approval.emailed"
        }
        for approval in await store.list_approvals(run.id):
            if approval.decision == "pending" and approval.token not in emailed:
                await self._send(store, run, approval)

    async def _send(self, store: RunStore, run: Run, approval: Approval) -> None:
        detail = {"step": approval.step, "tool": approval.tool, "token": approval.token,
                  "approver": approval.approver, "expires_at": approval.expires_at}  # fmt: skip
        try:
            await self._sender.send(self._message(run, approval))
            kind = "approval.emailed"
        except (EmailDeliveryError, ApprovalLinkInvalid, ValueError) as exc:
            kind = "approval.email_failed"
            detail["error"] = str(exc)
            log_event(kind, logging.WARNING, run_id=run.id, error=str(exc))
        event = AuditEvent(
            run_id=run.id, at=datetime.now(UTC), principal_id=run.principal_id, kind=kind,
            detail=detail,
        )  # fmt: skip
        await store.append_audit_event(event)

    def _message(self, run: Run, approval: Approval) -> EmailMessage:
        args = json.dumps(approval.args, indent=2, sort_keys=True, default=str)
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = approval.approver
        message["Subject"] = f"Approval needed: {approval.tool} for {run.principal_id}"
        message.set_content(
            f"{run.principal_id} asked {run.colleague} to run {run.protocol}, which stopped at "
            f"step {approval.step} and needs your approval to call {approval.tool} with:\n\n"
            f"{args[:MAX_ARGS_CHARS]}\n\n"
            f"Review and decide: {self.links.link(approval)}\n\n"
            f"The link works once and expires at {approval.expires_at}. If nobody decides "
            f"before then, the request goes to {run.colleague}'s escalation contact.\n"
        )
        return message
