"""Provider tests: one per acceptance criterion of issue #14, and the step_complete signal of
issue #44. No real model is called."""

from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import TextBlock, ToolUseBlock
from anthropic.types import Usage as SdkUsage

from cograil.domain import Step, Tool
from cograil.errors import ProviderError
from cograil.providers import (
    STEP_COMPLETE,
    AnthropicProvider,
    FakeProvider,
    Message,
    PlannedToolCall,
    StepComplete,
    scripted,
)
from cograil.providers.anthropic import STEP_COMPLETE_SPEC

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


def sdk_response(*extra: ToolUseBlock) -> SdkMessage:
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
            *extra,
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
    assert plan.step_complete is None
    request = client.messages.requests[0]
    assert request["tools"] == [
        {
            "name": "hris__get_balance",
            "description": TOOL.description,
            "input_schema": TOOL.args_schema,
        },
        STEP_COMPLETE_SPEC,
    ]
    assert "Find the leave balance." in request["system"][-1]["text"]


async def test_tool_not_offered_keeps_its_name_so_the_runner_can_reject_it() -> None:
    anthropic_provider, _ = provider(sdk_response())
    plan = await anthropic_provider.plan(STEP, [], [])
    assert plan.tool_calls[0].tool == "hris__get_balance"


async def test_plan_without_tools_or_context_still_sends_a_valid_request() -> None:
    anthropic_provider, client = provider(sdk_response())
    await anthropic_provider.plan(STEP, [], [])
    request = client.messages.requests[0]
    assert request["tools"] == [STEP_COMPLETE_SPEC]  # a Step can always end
    assert request["messages"][0]["role"] == "user"


async def test_a_step_complete_tool_use_becomes_the_structured_signal() -> None:
    done = ToolUseBlock(type="tool_use", id="toolu_2", name=STEP_COMPLETE, input={"output": "25"})
    anthropic_provider, _ = provider(sdk_response(done))
    plan = await anthropic_provider.plan(STEP, [], [TOOL])
    assert plan.tool_calls == [CALL]
    assert plan.step_complete == StepComplete(output="25")


async def test_the_runner_names_the_model_and_effort_per_call() -> None:
    anthropic_provider, client = provider(sdk_response())
    await anthropic_provider.plan(STEP, [], [], model="claude-haiku-4-5", effort="high")
    await anthropic_provider.plan(STEP, [], [])
    first, second = client.messages.requests
    assert (first["model"], first["extra_body"]) == (
        "claude-haiku-4-5",
        {"output_config": {"effort": "high"}},
    )
    assert second["model"] == "claude-sonnet-5-5" and "extra_body" not in second


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


@pytest.mark.parametrize(
    "names",
    [pytest.param(["a.b", "a__b"], id="each-other"), pytest.param([STEP_COMPLETE], id="reserved")],
)
async def test_tools_sharing_a_wire_name_raise_provider_error_before_any_call(
    names: list[str],
) -> None:
    tools = [TOOL.model_copy(update={"name": name}) for name in names]
    anthropic_provider, client = provider(sdk_response())
    with pytest.raises(ProviderError, match=names[-1]):
        await anthropic_provider.plan(STEP, [], tools)
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
