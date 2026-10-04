"""Orchestrator intent classification (issue #21) and audience pre-filter (issue #20).
No real model is called."""

import json
import logging
from typing import Any

import pytest

from cograil.domain import Audience, Colleague, Principal, Protocol, Step, Workspace
from cograil.orchestrator import ROUTE_TOOL, classification_model, classify_intent
from cograil.providers import FakeProvider, PlannedToolCall, scripted

STEP = Step(number=1, name="s", instruction="i")
ALICE = Principal(id="alice@example.com", groups=["staff"])


def workspace() -> Workspace:
    return Workspace(
        name="w",
        colleagues=[
            Colleague(
                name="harper",
                role="HR",
                escalation_contact="hr@example.com",
                protocols=["leave_request", "policy_question", "missing"],
            )
        ],
        protocols=[
            Protocol(name="leave_request", description="Request time off.", steps=[STEP]),
            Protocol(name="policy_question", description="Answer policy questions.", steps=[STEP]),
        ],
        tools=[],
    )


def route(**args: Any) -> FakeProvider:
    call = PlannedToolCall(id="t1", tool=ROUTE_TOOL, args=args)
    return FakeProvider([scripted("", call)])


async def test_classifies_to_colleague_and_protocol() -> None:
    provider = route(choice="harper/leave_request", confidence=0.93, reason="asks for leave")
    routing = await classify_intent(workspace(), ALICE, "I need next Friday off", provider)
    assert (routing.colleague, routing.protocol) == ("harper", "leave_request")
    assert routing.confidence == 0.93
    assert routing.refusal is None
    offered = provider.calls[0]
    assert [t.name for t in offered.tools] == [ROUTE_TOOL]
    assert offered.tools[0].args_schema["properties"]["choice"]["enum"] == [
        "harper/leave_request",
        "harper/policy_question",
        "none",
    ]
    assert offered.context[0].content == "I need next Friday off"


def test_classification_uses_the_small_tier() -> None:
    assert classification_model(workspace()) == "claude-haiku-4-5"


@pytest.mark.parametrize(
    "args",
    [
        {"choice": "none", "confidence": 0.8, "reason": "small talk"},
        {"choice": "harper/invented", "confidence": 0.9, "reason": "x"},
        {"choice": "harper/leave_request", "confidence": 1.7, "reason": "x"},
        {"choice": "harper/leave_request", "reason": "no confidence"},
    ],
)
async def test_none_returns_a_refusal_listing_what_the_workspace_can_do(
    args: dict[str, Any],
) -> None:
    routing = await classify_intent(workspace(), ALICE, "tell me a joke", route(**args))
    assert routing.protocol is None and routing.colleague is None
    assert routing.refusal is not None
    assert "harper / leave_request: Request time off." in routing.refusal
    assert "harper / policy_question: Answer policy questions." in routing.refusal
    assert "missing" not in routing.refusal


async def test_no_route_call_is_a_refusal() -> None:
    provider = FakeProvider([scripted("sure, here is a joke")])
    routing = await classify_intent(workspace(), ALICE, "tell me a joke", provider)
    assert routing.refusal is not None


async def test_a_tool_call_other_than_route_is_ignored() -> None:
    stray = PlannedToolCall(id="t0", tool="search_knowledge", args={"query": "leave policy"})
    call = PlannedToolCall(
        id="t1",
        tool=ROUTE_TOOL,
        args={"choice": "harper/leave_request", "confidence": 0.9, "reason": "asks for leave"},
    )
    provider = FakeProvider([scripted("", stray, call)])
    routing = await classify_intent(workspace(), ALICE, "I need next Friday off", provider)
    assert (routing.colleague, routing.protocol) == ("harper", "leave_request")
    assert routing.confidence == 0.9
    assert routing.refusal is None


async def test_the_first_of_several_route_calls_wins() -> None:
    first = PlannedToolCall(
        id="t1",
        tool=ROUTE_TOOL,
        args={"choice": "harper/leave_request", "confidence": 0.9, "reason": "asks for leave"},
    )
    second = PlannedToolCall(
        id="t2",
        tool=ROUTE_TOOL,
        args={"choice": "harper/policy_question", "confidence": 0.4, "reason": "policy"},
    )
    provider = FakeProvider([scripted("", first, second)])
    routing = await classify_intent(workspace(), ALICE, "I need next Friday off", provider)
    assert (routing.colleague, routing.protocol) == ("harper", "leave_request")
    assert routing.confidence == 0.9


async def test_classification_is_logged_with_confidence(caplog: pytest.LogCaptureFixture) -> None:
    provider = route(choice="harper/policy_question", confidence=0.71, reason="policy")
    with caplog.at_level(logging.INFO, logger="cograil"):
        await classify_intent(workspace(), ALICE, "how many sick days?", provider)
    events = [json.loads(r.message) for r in caplog.records]
    event = next(e for e in events if e["event"] == "orchestrator.classified")
    assert event["confidence"] == 0.71
    assert (event["colleague"], event["protocol"]) == ("harper", "policy_question")
    assert event["principal_id"] == "alice@example.com"
    assert event["message"] == "how many sick days?"
    assert event["model"] == "fake-model"


# Audience checks (issue #20): the closed list and the refusal are pre-filtered by audience.

MANAGER = Principal(id="mia@example.com", groups=["staff", "mgr"])
STAFF = Principal(id="sam@example.com", groups=["staff"])
OUTSIDER = Principal(id="olly@example.com", groups=["contractors"])
SCHEDULER = Principal(id="scheduler", kind="system")


def gated_workspace() -> Workspace:
    """harper (staff) runs leave_request (everyone) and approve_leave (managers, scheduled);
    finn (managers) runs policy_question (everyone)."""
    return Workspace(
        name="w",
        audiences=[
            Audience(name="staff", groups=["staff"]),
            Audience(name="managers", groups=["mgr"]),
        ],
        colleagues=[
            Colleague(
                name="harper",
                role="HR",
                escalation_contact="hr@example.com",
                protocols=["leave_request", "approve_leave"],
                audiences=["staff"],
            ),
            Colleague(
                name="finn",
                role="Finance",
                escalation_contact="finance@example.com",
                protocols=["policy_question"],
                audiences=["managers"],
            ),
        ],
        protocols=[
            Protocol(name="leave_request", description="Request time off.", steps=[STEP]),
            Protocol(
                name="approve_leave",
                description="Approve leave.",
                steps=[STEP],
                audiences=["managers"],
                scheduled_allowed=True,
            ),
            Protocol(name="policy_question", description="Answer policy questions.", steps=[STEP]),
        ],
        tools=[],
    )


def offered(provider: FakeProvider) -> list[str]:
    enum: list[str] = provider.calls[0].tools[0].args_schema["properties"]["choice"]["enum"]
    return enum


@pytest.mark.parametrize(
    ("principal", "labels"),
    [
        (MANAGER, ["harper/leave_request", "harper/approve_leave", "finn/policy_question"]),
        (STAFF, ["harper/leave_request"]),
        (SCHEDULER, ["harper/approve_leave"]),
    ],
    ids=["allowed-manager", "colleague-and-protocol-intersect", "scheduled-system-actor"],
)
async def test_the_model_is_offered_only_pairs_both_audiences_allow(
    principal: Principal, labels: list[str]
) -> None:
    provider = route(choice=labels[0], confidence=0.9, reason="fits")
    routing = await classify_intent(gated_workspace(), principal, "hello", provider)
    assert offered(provider) == [*labels, "none"]
    assert f"{routing.colleague}/{routing.protocol}" == labels[0]


async def test_a_principal_outside_the_audience_neither_sees_the_protocol_nor_errors() -> None:
    provider = route(choice="none", confidence=0.8, reason="not on the list")
    routing = await classify_intent(gated_workspace(), STAFF, "approve Bo's leave", provider)
    assert "harper/approve_leave" not in offered(provider)
    assert not routing.matched and routing.refusal is not None
    assert "approve_leave" not in routing.refusal and "finn" not in routing.refusal
    assert "harper / leave_request: Request time off." in routing.refusal
    assert "For anything else, contact hr@example.com." in routing.refusal
    assert "finance@example.com" not in routing.refusal


async def test_a_principal_outside_every_audience_gets_a_refusal_without_a_model_call() -> None:
    provider = FakeProvider([])
    routing = await classify_intent(gated_workspace(), OUTSIDER, "approve leave", provider)
    assert provider.calls == []
    assert routing.refusal is not None
    assert "nothing in this workspace is open to you" in routing.refusal
    assert "harper" not in routing.refusal and "@example.com" not in routing.refusal


async def test_an_empty_workspace_is_a_refusal_without_a_model_call() -> None:
    workspace = Workspace(name="w", colleagues=[], protocols=[], tools=[])
    provider = FakeProvider([])
    routing = await classify_intent(workspace, ALICE, "approve leave", provider)
    assert provider.calls == []
    assert routing.reason == "no protocols open to the principal"
    assert not routing.matched and routing.refusal is not None
    assert "nothing in this workspace is open to you" in routing.refusal
