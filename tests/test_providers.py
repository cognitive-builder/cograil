"""Provider tests: one per acceptance criterion of issue #14. No real model is called."""

from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import TextBlock, ToolUseBlock
from anthropic.types import Usage as SdkUsage

from cograil.domain import Colleague, Protocol, Step, Tool
from cograil.errors import ProviderError
from cograil.providers import (
    AnthropicProvider,
    FakeProvider,
    Message,
    PlannedToolCall,
    resolve_model,
    scripted,
)

STEP = Step(number=1, name="Look up balance", instruction="Find the leave balance.")
TOOL = Tool(
    name="hris.get_balance",
    kind="python",
    scope="read",
    description="Read a leave balance",
    args_schema={"type": "object", "properties": {"employee": {"type": "string"}}},
)
CALL = PlannedToolCall(id="toolu_1", tool="hris.get_balance", args={"employee": "alice"})


class StubMessages:
    def __init__(self, response: SdkMessage | Exception) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SdkMessage:
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class StubClient:
    def __init__(self, response: SdkMessage | Exception) -> None:
        self.messages = StubMessages(response)


def sdk_response() -> SdkMessage:
    return SdkMessage(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-sonnet-5-5",
        content=[
            TextBlock(type="text", text="Checking. "),
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="hris__get_balance", input={"employee": "alice"}
            ),
        ],
        stop_reason="tool_use",
        stop_sequence=None,
        usage=SdkUsage(input_tokens=120, output_tokens=34),
    )


def provider(response: SdkMessage | Exception) -> tuple[AnthropicProvider, StubClient]:
    client = StubClient(response)
    return AnthropicProvider("claude-sonnet-5-5", client=client), client  # type: ignore[arg-type]


async def test_plan_returns_text_and_structured_tool_calls() -> None:
    anthropic_provider, client = provider(sdk_response())
    plan = await anthropic_provider.plan(STEP, [Message(role="user", content="Alice asks")], [TOOL])
    assert plan.text == "Checking. "
    assert plan.tool_calls == [CALL]
    request = client.messages.requests[0]
    assert request["tools"] == [
        {
            "name": "hris__get_balance",
            "description": TOOL.description,
            "input_schema": TOOL.args_schema,
        }
    ]
    assert "Find the leave balance." in request["system"]


async def test_tool_not_offered_keeps_its_name_so_the_runner_can_reject_it() -> None:
    anthropic_provider, _ = provider(sdk_response())
    plan = await anthropic_provider.plan(STEP, [], [])
    assert plan.tool_calls[0].tool == "hris__get_balance"


async def test_plan_without_tools_or_context_still_sends_a_valid_request() -> None:
    anthropic_provider, client = provider(sdk_response())
    await anthropic_provider.plan(STEP, [], [])
    request = client.messages.requests[0]
    assert "tools" not in request
    assert request["messages"][0]["role"] == "user"


@pytest.mark.parametrize(
    ("protocol_model", "expected"),
    [("claude-opus-5-5", "claude-opus-5-5"), (None, "claude-haiku-4-5")],
)
async def test_model_comes_from_protocol_or_colleague_policy(
    protocol_model: str | None, expected: str
) -> None:
    protocol = Protocol(name="p", steps=[STEP], model=protocol_model)
    colleague = Colleague(
        name="c", role="r", escalation_contact="x@example.com", protocols=["p"],
        model_policy="claude-haiku-4-5",
    )  # fmt: skip
    assert resolve_model(protocol, colleague) == expected
    client = StubClient(sdk_response())
    built = AnthropicProvider.for_protocol(protocol, colleague, client=client)  # type: ignore[arg-type]
    await built.plan(STEP, [], [])
    assert client.messages.requests[0]["model"] == expected


async def test_usage_is_returned_on_every_call() -> None:
    anthropic_provider, _ = provider(sdk_response())
    plan = await anthropic_provider.plan(STEP, [], [TOOL])
    assert (plan.usage.input_tokens, plan.usage.output_tokens) == (120, 34)
    fake_plan = await FakeProvider([scripted("done", input_tokens=7, output_tokens=3)]).plan(
        STEP, [], []
    )
    assert (fake_plan.usage.input_tokens, fake_plan.usage.output_tokens) == (7, 3)


async def test_sdk_errors_become_provider_errors() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    anthropic_provider, _ = provider(anthropic.APIConnectionError(request=request))
    with pytest.raises(ProviderError):
        await anthropic_provider.plan(STEP, [], [])


async def test_tools_sharing_a_wire_name_raise_provider_error_before_any_call() -> None:
    dotted = TOOL.model_copy(update={"name": "a.b"})
    underscored = TOOL.model_copy(update={"name": "a__b"})
    anthropic_provider, client = provider(sdk_response())
    with pytest.raises(ProviderError, match="a__b"):
        await anthropic_provider.plan(STEP, [], [dotted, underscored])
    assert client.messages.requests == []


async def test_fake_provider_replays_scripted_tool_calls_in_order() -> None:
    fake = FakeProvider([scripted("looking", CALL), scripted("all done")])
    first = await fake.plan(STEP, [], [TOOL])
    second = await fake.plan(STEP, [Message(role="user", content="result")], [TOOL])
    assert first.tool_calls == [CALL] and first.stop_reason == "tool_use"
    assert second.text == "all done" and second.tool_calls == []
    assert [len(c.context) for c in fake.calls] == [0, 1]
    assert fake.calls[0].tools == [TOOL]


async def test_fake_provider_fails_loudly_when_the_script_runs_out() -> None:
    fake = FakeProvider([scripted("only one")])
    await fake.plan(STEP, [], [])
    with pytest.raises(ProviderError):
        await fake.plan(STEP, [], [])
