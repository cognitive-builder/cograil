"""check_audience: the Colleague's and the Protocol's audiences must both allow the principal."""

import pytest

from cograil.audience import check_audience
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
