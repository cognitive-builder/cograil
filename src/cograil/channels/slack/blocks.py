"""The messages the Slack channel posts: an approval prompt with buttons, and a Run's outcome."""

from __future__ import annotations

from typing import Any

from cograil.api.schemas import PendingApproval, RunOutcome
from cograil.channels.slack.identity import escape
from cograil.domain import RunStatus

APPROVE_ACTION = "cograil_approve"
DECLINE_ACTION = "cograil_decline"
MAX_TEXT = 3000  # a section block holds at most 3000 characters


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def approval_blocks(approval: PendingApproval, mention: str) -> list[dict[str, Any]]:
    """The prompt for one gate. The buttons carry the Approval token and nothing else: the
    decider is whoever clicks, mapped to a Principal, and the Runner refuses anyone but the
    approver."""
    text = f"*Approval needed* for `{escape(approval.tool)}` (step {approval.step}). "
    text += f"Approver: {mention}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                _button("Approve", APPROVE_ACTION, approval.token, "primary"),
                _button("Decline", DECLINE_ACTION, approval.token, "danger"),
            ],
        },
    ]


def _button(label: str, action_id: str, token: str, style: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": label},
        "action_id": action_id,
        "value": token,
        "style": style,
    }


def outcome_text(outcome: RunOutcome) -> str | None:
    """What to tell the thread about a Run that stopped; None when the approval prompts say
    it all."""
    status = outcome.run.status
    if status is RunStatus.completed:
        return clip(escape(outcome.output)) if outcome.output else "Done."
    if status is RunStatus.awaiting_approval:
        return None
    if status is RunStatus.escalated:
        return "This run was escalated to a person. The audit log says why."
    if status is RunStatus.failed:
        return "This run failed. The audit log says why."
    return f"This run is {status.value.replace('_', ' ')}."
