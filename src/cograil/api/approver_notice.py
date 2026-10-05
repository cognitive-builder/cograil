"""Tell the approver of a paused Run, on every channel that can reach them (issue #263).

A Run that stops at a Gate waits for its approver, and a scheduled Run has no chat to say so.
`notify_approvers` is the one function every path calls (chat, Slack, scheduled, a resumed Run
that stops again): it offers each pending Approval to each `ApproverChannel` (email when mail is
configured, a Slack direct message when Slack is on and the approver has a `slack_id`) once.
An Approval no channel reached is an `approval.undeliverable` AuditEvent with the Run's
principal, and is returned so that the caller can escalate a Run nobody is watching.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from cograil.domain import Approval, AuditEvent, Run, RunStatus
from cograil.store import RunStore

# A channel that reached the approver writes one of these for the Approval's token, which is
# how the same Approval is never sent twice.
NOTIFIED_KINDS = frozenset({"approval.emailed", "approval.slack_sent", "approval.undeliverable"})


class ApproverChannel(Protocol):
    """One way to reach an approver; a Services holds the ones the deployment has."""

    name: str

    async def send(self, store: RunStore, run: Run, approval: Approval) -> bool:
        """Tell the approver of `approval`; True when it reached them. Writes its own
        AuditEvent for the outcome, and never raises: a failed send is a False."""
        ...


async def notify_approvers(
    store: RunStore, channels: Sequence[ApproverChannel], run: Run
) -> list[Approval]:
    """Offer each pending Approval of `run` that nobody was told about to every channel.

    Returns the Approvals no channel reached, each recorded as `approval.undeliverable`."""
    if run.status is not RunStatus.awaiting_approval:
        return []
    told = {
        e.detail.get("token") for e in await store.list_audit_events(run.id)
        if e.kind in NOTIFIED_KINDS
    }  # fmt: skip
    unreached: list[Approval] = []
    for approval in await store.list_approvals(run.id):
        if approval.decision != "pending" or approval.token in told:
            continue
        reached = [await channel.send(store, run, approval) for channel in channels]
        if not any(reached):
            await store.append_audit_event(_undeliverable(run, approval, channels))
            unreached.append(approval)
    return unreached


def _undeliverable(run: Run, approval: Approval, channels: Sequence[ApproverChannel]) -> AuditEvent:
    detail = {"step": approval.step, "tool": approval.tool, "token": approval.token,
              "approver": approval.approver,
              "tried": [channel.name for channel in channels]}  # fmt: skip
    return AuditEvent(
        run_id=run.id, at=datetime.now(UTC), principal_id=run.principal_id,
        kind="approval.undeliverable", detail=detail,
    )  # fmt: skip
