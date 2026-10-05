"""The messages the Slack channel posts: an approval prompt with buttons, and a Run's outcome."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cograil.api.schemas import RunOutcome
from cograil.channels.slack.identity import escape
from cograil.domain import Approval, RunStatus

APPROVE_ACTION = "cograil_approve"
DECLINE_ACTION = "cograil_decline"
MAX_TEXT = 3000  # a section block holds at most 3000 characters
MAX_ARGS = 1000  # as the approval email clips the arguments
CLIPPED_POINTER = "The rest is on the approval page; approve it there, where all of it is shown."


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _escape_within(text: str, limit: int) -> str:
    """`text` escaped, cut where the escaped form reaches `limit` characters; never half of an
    entity like `&amp;`, which Slack would show as stray characters."""
    kept: list[str] = []
    room = limit
    for char in text:
        piece = escape(char)
        if len(piece) > room:
            break
        kept.append(piece)
        room -= len(piece)
    return "".join(kept)


def args_text(args: Mapping[str, Any]) -> tuple[str, bool]:
    """The call's arguments as the approval page shows them, clipped so that even escaped
    (`&` becomes `&amp;`) they stay within MAX_ARGS characters, and whether they were clipped.
    Clipped arguments point to the page, the only place they can be approved."""
    shown = json.dumps(args, indent=2, sort_keys=True, default=str)
    escaped = escape(shown)
    body = escaped if len(escaped) <= MAX_ARGS else _escape_within(shown, MAX_ARGS)
    clipped = len(body) < len(escaped)
    # A code fence inside the arguments must not close ours and let the rest format as text.
    body = body.replace("```", "``​`")
    tail = "\n…" if clipped else ""
    pointer = f"\n{CLIPPED_POINTER}" if clipped else ""
    return f"```{body}{tail}```{pointer}", clipped


def approval_blocks(approval: Approval, requester: str) -> list[dict[str, Any]]:
    """The prompt for one gate, posted to its approver: who asked, the Tool, the Step and the
    call's arguments. The buttons carry the Approval token and nothing else: the decider is
    whoever clicks, mapped to a Principal, and the Runner refuses anyone but the approver.
    When the arguments are clipped there is no Approve button: padding in one argument must
    not push another out of sight of the person approving it (docs/threat-model.md)."""
    shown, clipped = args_text(approval.args)
    text = f"*Approval needed.* {requester} asked for a call to `{escape(approval.tool)}` "
    text += f"(step {approval.step}) with:\n{shown}"
    buttons = [] if clipped else [_button("Approve", APPROVE_ACTION, approval.token, "primary")]
    buttons.append(_button("Decline", DECLINE_ACTION, approval.token, "danger"))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": buttons},
    ]


def _button(label: str, action_id: str, token: str, style: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": label},
        "action_id": action_id,
        "value": token,
        "style": style,
    }


def waiting_text(approver: str) -> str:
    """What the Run's thread says while an approver has the prompt: no buttons."""
    return f"Waiting for {approver} to approve."


def no_slack_approver_text(approver: str) -> str:
    """What the thread says when the approver has no `slack_id`, so nothing reached them."""
    return (
        f"Waiting for {approver} to approve. They have no Slack account in principals.yaml, so "
        "nothing was sent to them in Slack: they decide with the link in their email or on the "
        "approval page."
    )


def undelivered_prompt_text(approver: str) -> str:
    """What the thread says when Slack refused the prompt (a missing scope, a deactivated
    member): the approver has a Slack account, but they must decide elsewhere."""
    return (
        f"Waiting for {approver} to approve. The prompt could not be delivered to them in Slack; "
        "they decide with the link in their email or on the approval page."
    )


def decided_text(approval: Approval, user: str) -> str:
    """What replaces the prompt after a click: the outcome the Runner recorded, not the button
    (an Approval past its deadline is recorded as expired and the Run escalates)."""
    call = f"`{escape(approval.tool)}` (step {approval.step})"
    if approval.decision == "approved":
        return f"Approved by <@{user}>: {call}."
    if approval.decision == "declined":
        return f"Declined by <@{user}>: {call}."
    return (
        f"Expired: this approval for {call} was past its deadline when <@{user}> answered, so it "
        "was recorded as expired and the run was escalated. Nothing was approved."
    )


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
