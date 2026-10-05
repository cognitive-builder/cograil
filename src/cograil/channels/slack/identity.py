"""Slack member id to Principal, through `slack_id` in the workspace's principals.yaml.

The Principal's groups come from the same file, so the Slack channel entitles a person exactly
as the web chat does (rule 3). A member id no principal names is nobody: the channel answers
them and starts nothing.
"""

from __future__ import annotations

from cograil.audience import resolve_principal
from cograil.domain import Principal, Workspace
from cograil.identity import same_principal


def principal_for_slack_user(workspace: Workspace, slack_id: str) -> Principal | None:
    """The workspace's Principal whose `slack_id` is `slack_id`, or None."""
    known = next((p for p in workspace.principals if p.slack_id == slack_id), None)
    return resolve_principal(workspace, known.id) if known else None


def slack_id_of(workspace: Workspace, principal_id: str) -> str | None:
    """The `slack_id` principals.yaml gives a principal: the map of `principal_for_slack_user`,
    reversed. None when the principal is not listed or has no Slack id."""
    known = next((p for p in workspace.principals if same_principal(p.id, principal_id)), None)
    return known.slack_id if known else None


def slack_mention(workspace: Workspace, principal_id: str) -> str:
    """How to name a principal in a message: `<@U...>` if they have a Slack id, else their id."""
    slack_id = slack_id_of(workspace, principal_id)
    return f"<@{slack_id}>" if slack_id else escape(principal_id)


def escape(text: str) -> str:
    """Slack's escaping of `&`, `<` and `>`, so model output or a name cannot make a mention
    (`<!channel>`) or a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
