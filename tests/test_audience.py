"""check_audience: the Colleague's and the Protocol's audiences must both allow the principal."""

import pytest

from cograil.audience import audience_denial, check_audience, resolve_principal
from cograil.domain import Audience, Colleague, Principal, Protocol, Step, Workspace
from cograil.errors import AudienceDenied

WORKSPACE = Workspace(
    name="w",
    colleagues=[],
    protocols=[],
    tools=[],
    audiences=[Audience(name="staff", groups=["staff"]), Audience(name="managers", groups=["mgr"])],
)


def colleague(*audiences: str) -> Colleague:
    return Colleague(
        name="c", role="r", escalation_contact="boss", protocols=["p"], audiences=list(audiences)
    )


def protocol(*audiences: str, manual: bool = True) -> Protocol:
    step = Step(number=1, name="s", instruction="i")
    return Protocol(name="p", steps=[step], audiences=list(audiences), manual_allowed=manual)


@pytest.mark.parametrize(
    ("groups", "colleague_audiences", "protocol_audiences", "manual", "allowed"),
    [
        ([], ["everyone"], ["everyone"], True, True),
        (["staff"], ["staff"], ["staff"], True, True),
        ([], ["staff"], ["everyone"], True, False),
        (["staff"], ["staff"], ["managers"], True, False),
        (["staff", "mgr"], ["staff"], ["managers"], True, True),
        (["staff"], ["staff"], ["staff"], False, False),
    ],
    ids=["everyone", "member", "colleague-denies", "protocol-denies", "both-allow", "no-manual"],
)
def test_both_audiences_and_manual_execution_must_allow(
    groups: list[str],
    colleague_audiences: list[str],
    protocol_audiences: list[str],
    manual: bool,
    allowed: bool,
) -> None:
    principal = Principal(id="p@example.com", groups=groups)
    args = (
        WORKSPACE,
        colleague(*colleague_audiences),
        protocol(*protocol_audiences, manual=manual),
    )
    if allowed:
        check_audience(*args, principal)
    else:
        with pytest.raises(AudienceDenied):
            check_audience(*args, principal)


@pytest.mark.parametrize(("scheduled", "allowed"), [(True, True), (False, False)])
def test_the_system_actor_needs_scheduled_execution_not_an_audience(
    scheduled: bool, allowed: bool
) -> None:
    system = Principal(id="scheduler", kind="system")
    gated = protocol("managers", manual=False).model_copy(update={"scheduled_allowed": scheduled})
    assert (audience_denial(WORKSPACE, colleague("staff"), gated, system) is None) is allowed


DIRECTORY = WORKSPACE.model_copy(
    update={
        "audiences": [
            Audience(name="staff", groups=["staff"], claims=["0f1e-staff-guid"]),
            Audience(name="managers", groups=["mgr", "staff"], claims=["managers@example.com"]),
        ],
        "principals": [
            Principal(id="Alice@Example.com", aliases=["a.smith@corp.example.com"], groups=["hr"])
        ],
    }
)


@pytest.mark.parametrize(
    ("signed_in", "claims", "expected"),
    [
        ("ALICE@example.com ", [], ("alice@example.com", ["hr"])),
        ("a.smith@CORP.example.com", ["0f1e-staff-guid"], ("alice@example.com", ["hr", "staff"])),
        ("bob@example.com", ["managers@example.com"], ("bob@example.com", ["mgr", "staff"])),
        ("bob@example.com", ["unmapped", "0F1E-STAFF-GUID"], ("bob@example.com", [])),
    ],
    ids=["by-id-any-case", "by-alias-with-claim", "unknown-with-claim", "unmapped-claims"],
)
def test_resolve_principal_maps_ids_aliases_and_group_claims(
    signed_in: str, claims: list[str], expected: tuple[str, list[str]]
) -> None:
    """Issue #19: one Principal per person; group claims reach groups only through Audiences."""
    principal = resolve_principal(DIRECTORY, signed_in, claims=claims)
    assert (principal.id, principal.groups) == expected
