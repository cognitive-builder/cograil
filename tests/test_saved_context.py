"""Saved context tests for issue #54: one per acceptance criterion (ADR 0013)."""

from typing import Any

import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import TextBlock
from anthropic.types import Usage as SdkUsage

from cograil.context import ContextBuilder, cache_ledger, format_ledger, window_ledger
from cograil.domain import Colleague, Harness, Price, Step
from cograil.harness import call_cost
from cograil.parser import parse_protocol
from cograil.providers import AnthropicProvider, FakeProvider, Message, scripted
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

HARPER = Colleague(
    name="harper", role="the HR colleague", escalation_contact="hr@example.com", protocols=["demo"]
)
PROTOCOL = """
Protocol: demo
1. Step "One": Look at the request.
2. Step "Two": Answer it.
Guardrails:
- Never share salaries.
"""
STEP = Step(number=1, name="One", instruction="Look at the request.")
PREFIX = "You are harper. Protocol demo."


async def run_demo(store: InMemoryRunStore, script: list[Any]) -> FakeProvider:
    provider = FakeProvider(script)
    runner = Runner(provider, ToolRegistry(store), store, HARPER)
    await runner.run("r1", parse_protocol(PROTOCOL))
    return provider


async def test_two_steps_of_one_run_send_an_identical_prefix(store: InMemoryRunStore) -> None:
    provider = await run_demo(store, [scripted("RESULT-ONE", done=True), scripted("ok", done=True)])
    first, second = provider.calls
    assert (first.step.number, second.step.number) == (1, 2)
    assert first.prefix == second.prefix != ""
    assert "harper" in first.prefix and "hr@example.com" in first.prefix  # persona
    assert "Answer it." in first.prefix and "Never share salaries." in first.prefix  # protocol
    assert "RESULT-ONE" in second.context[0].content  # per-run data comes after the prefix
    assert "RESULT-ONE" not in second.prefix


def test_the_prefix_depends_on_the_colleague_and_protocol_only() -> None:
    builder, protocol = ContextBuilder(Harness()), parse_protocol(PROTOCOL)
    assert builder.prefix(HARPER, protocol) == builder.prefix(HARPER, protocol)
    other = HARPER.model_copy(update={"name": "sam"})
    assert builder.prefix(other, protocol) != builder.prefix(HARPER, protocol)


class StubMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SdkMessage:
        self.requests.append(kwargs)
        return SdkMessage(
            id="msg_1", type="message", role="assistant", model="claude-sonnet-5-5",
            content=[TextBlock(type="text", text="hi")], stop_reason="end_turn",
            stop_sequence=None,
            usage=SdkUsage(input_tokens=7, output_tokens=3, cache_read_input_tokens=900,
                           cache_creation_input_tokens=120),
        )  # fmt: skip


class StubClient:
    def __init__(self) -> None:
        self.messages = StubMessages()


async def test_the_anthropic_provider_marks_the_stable_prefix_for_caching() -> None:
    client = StubClient()
    provider = AnthropicProvider("claude-sonnet-5-5", client=client)  # type: ignore[arg-type]
    plan = await provider.plan(STEP, [Message(role="user", content="data")], [], prefix=PREFIX)
    system = client.messages.requests[0]["system"]
    assert system[0] == {"type": "text", "text": PREFIX, "cache_control": {"type": "ephemeral"}}
    assert "Look at the request." in system[1]["text"] and "cache_control" not in system[1]
    assert (plan.usage.cache_read_tokens, plan.usage.cache_write_tokens) == (900, 120)
    assert plan.usage.input_tokens == 7


async def test_without_a_prefix_nothing_is_marked() -> None:
    client = StubClient()
    provider = AnthropicProvider("claude-sonnet-5-5", client=client)  # type: ignore[arg-type]
    await provider.plan(STEP, [], [])
    assert all("cache_control" not in block for block in client.messages.requests[0]["system"])


async def test_cache_tokens_are_recorded_per_call_and_shown_in_the_ledger(
    store: InMemoryRunStore,
) -> None:
    await run_demo(
        store,
        [
            scripted("one", done=True, cache_write_tokens=500),
            scripted("two", done=True, cache_read_tokens=500),
        ],
    )
    run = await store.get_run("r1")
    assert cache_ledger(run) == {1: (0, 500), 2: (500, 0)}
    lines = format_ledger(window_ledger(run), cache=cache_ledger(run))
    assert "cache_read  cache_write" in lines[0]
    assert lines[1].endswith(f"{0:>10}  {500:>11}") and lines[-1].endswith(f"{500:>10}  {500:>11}")
    assert "cache_read" not in format_ledger(window_ledger(run))[0]


async def test_a_run_without_caching_has_no_cache_columns(store: InMemoryRunStore) -> None:
    await run_demo(store, [scripted("one", done=True), scripted("two", done=True)])
    assert cache_ledger(await store.get_run("r1")) == {}


@pytest.mark.parametrize(
    ("read", "write", "expected"),
    [(0, 0, 1.0), (1_000_000, 0, 1.1), (0, 1_000_000, 2.25)],
)
def test_cache_tokens_are_priced_by_their_factors_of_the_input_price(
    read: int, write: int, expected: float
) -> None:
    harness = Harness(pricing={"m": Price(input_per_mtok=1.0, output_per_mtok=0.0)})
    assert call_cost(harness, "m", 1_000_000, 0, read, write) == pytest.approx(expected)
