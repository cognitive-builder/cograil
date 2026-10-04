"""Audience check: may this principal start this Protocol with this Colleague?

An Audience name is allowed when it is `everyone` or one of the workspace's Audiences whose
groups include one of the principal's groups. The Colleague's audiences and the Protocol's
audiences must both allow the principal, and a Protocol must allow manual execution.
"""

from __future__ import annotations

from cograil.domain import Colleague, Principal, Protocol, Workspace
from cograil.errors import AudienceDenied

EVERYONE = "everyone"


def check_audience(
    workspace: Workspace, colleague: Colleague, protocol: Protocol, principal: Principal
) -> None:
    """Raise AudienceDenied unless the principal may start the Protocol by hand."""
    if not protocol.manual_allowed:
        raise AudienceDenied(f"protocol {protocol.name} does not allow manual execution")
    groups = set(principal.groups)
    allowed = {EVERYONE} | {a.name for a in workspace.audiences if groups & set(a.groups)}
    for kind, name, audiences in (
        ("colleague", colleague.name, colleague.audiences),
        ("protocol", protocol.name, protocol.audiences),
    ):
        if not allowed & set(audiences):
            raise AudienceDenied(
                f"{principal.id} is outside the audience of {kind} {name} ({', '.join(audiences)})"
            )
