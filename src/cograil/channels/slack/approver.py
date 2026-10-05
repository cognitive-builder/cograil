"""The Slack direct message that tells an approver a gate waits on them (issue #257, #263).

One of the channels of `notify_approvers` (cograil.api.approver_notice), so a Run started in the
web chat, in Slack or by a Schedule reaches an approver who has a `slack_id` the same way. The
prompt names the requester, the Tool, the Step and the call's arguments, with the buttons that
`SlackChannel.on_decision` handles. Each outcome is an AuditEvent of the Run, `approval.slack_sent`
or `approval.slack_failed`, naming the approver and never the arguments. An approver with no
`slack_id` is not reachable here: nothing is sent and nothing is written.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cograil.channels.slack.blocks import approval_blocks
from cograil.channels.slack.channel import Poster
from cograil.channels.slack.identity import slack_id_of, slack_mention
from cograil.domain import Approval, AuditEvent, Run, Workspace
from cograil.observability import log_event
from cograil.store import RunStore


class SlackApprover:
    name = "slack"

    def __init__(self, workspace: Workspace, poster: Poster) -> None:
        self._workspace = workspace
        self._poster = poster

    async def send(self, store: RunStore, run: Run, approval: Approval) -> bool:
        """Send the approver the gate in a direct message; True when Slack took it."""
        slack_id = slack_id_of(self._workspace, approval.approver)
        if slack_id is None:
            return False
        detail = {"step": approval.step, "tool": approval.tool, "token": approval.token,
                  "approver": approval.approver}  # fmt: skip
        try:
            dm = await self._poster.open_dm(slack_id)
            blocks = approval_blocks(approval, slack_mention(self._workspace, run.principal_id))
            await self._poster.post(dm, None, f"Approval needed for {approval.tool}", blocks)
            kind = "approval.slack_sent"
        except Exception as exc:  # Slack refused (a missing scope, a deactivated member)
            kind = "approval.slack_failed"
            detail["error"] = type(exc).__name__
            log_event("slack.prompt_undelivered", logging.WARNING, error=type(exc).__name__)
        event = AuditEvent(
            run_id=run.id, at=datetime.now(UTC), principal_id=run.principal_id, kind=kind,
            detail=detail,
        )  # fmt: skip
        await store.append_audit_event(event)
        return kind == "approval.slack_sent"
