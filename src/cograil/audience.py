"""Audience check: may this principal start this Protocol with this Colleague?

An Audience name is allowed when it is `everyone` or one of the workspace's Audiences whose
groups include one of the principal's groups. For a user, the Colleague's audiences and the
Protocol's audiences must both allow the principal, and the Protocol must allow manual
execution. The system actor (a `Principal` of kind `system`, which starts scheduled Runs) has
no groups: it is allowed exactly when the Protocol allows scheduled execution.

The CLI and the orchestrator share this rule; neither keeps its own copy.

`resolve_principal` is the one way a signed-in id becomes a Principal: it looks the id up in
the workspace's principals by id or alias, and adds the groups of every Audience whose
`claims` include one of the identity provider's group claim values.
"""

from __future__ import annotations

from collections.abc import Iterable

from cograil.domain import Colleague, Principal, Protocol, Workspace
from cograil.errors import AudienceDenied
from cograil.identity import normalise_principal_id

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


def resolve_principal(
    workspace: Workspace,
    principal_id: str,
    *,
    claims: Iterable[str] = (),
    groups: Iterable[str] = (),
) -> Principal:
    """The workspace's Principal for this id or alias, with the groups that `claims` map to
    through Audiences and any extra `groups` added. An unknown id is a Principal of its own."""
    wanted = normalise_principal_id(principal_id)
    known = next((p for p in workspace.principals if wanted in (p.id, *p.aliases)), None)
    principal = known or Principal(id=wanted)
    claimed = set(claims)
    mapped = [g for a in workspace.audiences if claimed & set(a.claims) for g in a.groups]
    merged = sorted({*principal.groups, *mapped, *groups})
    return principal.model_copy(update={"groups": merged})
