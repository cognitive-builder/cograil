"""Audience check: may this principal start this Protocol with this Colleague?

An Audience name is allowed when it is `everyone` or one of the workspace's Audiences whose
groups include one of the principal's groups. For a user, the Colleague's audiences and the
Protocol's audiences must both allow the principal, and the Protocol must allow manual
execution. The system actor (a `Principal` of kind `system`, which starts scheduled Runs) has
no groups: it is allowed exactly when the Protocol allows scheduled execution.

The CLI and the orchestrator share this rule; neither keeps its own copy.
"""

from __future__ import annotations

from cograil.domain import Colleague, Principal, Protocol, Workspace
from cograil.errors import AudienceDenied

EVERYONE = "everyone"


def audience_denial(
    workspace: Workspace, colleague: Colleague, protocol: Protocol, principal: Principal
) -> str | None:
    """Why the principal may not start the Protocol with the Colleague, or None if it may."""
    if principal.kind == "system":
        if protocol.scheduled_allowed:
            return None
        return f"protocol {protocol.name} does not allow scheduled execution"
    if not protocol.manual_allowed:
        return f"protocol {protocol.name} does not allow manual execution"
    groups = set(principal.groups)
    allowed = {EVERYONE} | {a.name for a in workspace.audiences if groups & set(a.groups)}
    for kind, name, audiences in (
        ("colleague", colleague.name, colleague.audiences),
        ("protocol", protocol.name, protocol.audiences),
    ):
        if not allowed & set(audiences):
            listed = ", ".join(audiences)
            return f"{principal.id} is outside the audience of {kind} {name} ({listed})"
    return None


def check_audience(
    workspace: Workspace, colleague: Colleague, protocol: Protocol, principal: Principal
) -> None:
    """Raise AudienceDenied unless the principal may start the Protocol."""
    if reason := audience_denial(workspace, colleague, protocol, principal):
        raise AudienceDenied(reason)
