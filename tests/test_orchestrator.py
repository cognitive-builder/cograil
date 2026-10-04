"""Orchestrator intent classification: one test per acceptance criterion of issue #21.
No real model is called."""

import json
import logging
from typing import Any

import pytest

from cograil.domain import Colleague, Principal, Protocol, Step, Workspace
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
