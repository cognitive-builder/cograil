"""Runner tests for issue #10: whitelist, gates, context, cursor, bounds, invoke's only caller."""

import ast
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import ToolUseBlock
from anthropic.types import Usage as SdkUsage

from cograil.domain import Approval, Protocol, RunStatus, Tool
from cograil.errors import GateRequired, LoopBudgetExceeded, ProviderError, ToolNotAllowed
from cograil.parser import parse_protocol
from cograil.providers import AnthropicProvider, FakeProvider, Plan, PlannedToolCall, scripted
from cograil.registry import ToolRegistry
from cograil.runner import DATA, Runner
from cograil.store import InMemoryRunStore

SRC = Path(__file__).parents[1] / "src/cograil"
SUBMIT = {"employee": "alice", "days": 3}
PROTOCOL = """
Protocol: demo
1. Step "Look up": Use @hris.get_balance for the requester. (turns: 2)
2. Step "Submit": Use @hris.submit_leave with the confirmed dates.
3. Step "Notify": Use @notify.send to tell the requester. (context: steps 1, 2)
"""
TOOLS = [
    Tool(name="hris.get_balance", kind="python", scope="read"),
    Tool(name="hris.submit_leave", kind="python", scope="write", confirm_before_write=True),
    Tool(name="notify.send", kind="python", scope="write", confirm_before_write=False),
]


def call(tool: str, **args: Any) -> PlannedToolCall:
    return PlannedToolCall(id=f"call-{tool}", tool=tool, args=args)


LOOK_UP = [scripted("", call("hris.get_balance")), scripted("25 days left")]
SUBMITTED = [scripted("", call("hris.submit_leave", **SUBMIT)), scripted("submitted")]
NOTIFIED = [scripted("", call("notify.send")), scripted("told alice")]


@pytest.fixture
def protocol() -> Protocol:
    return parse_protocol(PROTOCOL)


@pytest.fixture
def invoked() -> list[str]:
    return []


@pytest.fixture
def registry(store: InMemoryRunStore, invoked: list[str]) -> ToolRegistry:
    registry = ToolRegistry(store)
    for tool in TOOLS:

        async def invoke(args: dict[str, Any], name: str = tool.name) -> dict[str, Any]:
            invoked.append(name)
            return {"tool": name, "ok": True}

        registry.register(tool, invoke)
    return registry


async def approve(store: InMemoryRunStore, token: str = "a1") -> None:
    await store.create_approval(
        Approval(token=token, run_id="r1", step=2, tool="hris.submit_leave", args=SUBMIT,
                 approver="bob", decision="approved")
    )  # fmt: skip


async def run(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, script: list[Plan]
) -> FakeProvider:
    provider = FakeProvider(script)
    await Runner(provider, registry, store).run("r1", protocol)
    return provider


async def assert_failed_closed(
    store: InMemoryRunStore, error: type[Exception], cursor: int
) -> None:
    stored = await store.get_run("r1")
    assert (stored.status, stored.cursor) == (RunStatus.failed, cursor)
    last = (await store.list_audit_events("r1"))[-1]
    assert (last.kind, last.principal_id) == ("run.failed", "alice@example.com")
    assert last.detail["error"] == error.__name__


@pytest.mark.parametrize(
    "planned",
    [
        pytest.param([call("hris.submit_leave", **SUBMIT)], id="registered-but-not-listed"),
        pytest.param([call("hris__get_balance")], id="unmapped-wire-name"),
        pytest.param([call("hris.get_balance"), call("notify.send")], id="listed-beside-unlisted"),
    ],
)
async def test_off_whitelist_call_raises_tool_not_allowed_and_fails_closed(
    store: InMemoryRunStore,
    registry: ToolRegistry,
    protocol: Protocol,
    invoked: list[str],
    planned: list[PlannedToolCall],
) -> None:
    await approve(store)
    with pytest.raises(ToolNotAllowed):
        await run(store, registry, protocol, [scripted("", *planned)])
    assert invoked == []
    assert await store.list_tool_calls("r1") == []
    await assert_failed_closed(store, ToolNotAllowed, cursor=0)


class StubAnthropic:
    """An SDK client that answers every request with one tool_use block."""

    def __init__(self, wire_name: str) -> None:
        self.messages = self
        self.wire_name = wire_name

    async def create(self, **kwargs: Any) -> SdkMessage:
        block = ToolUseBlock(type="tool_use", id="toolu_1", name=self.wire_name, input={})
        return SdkMessage(id="msg_1", type="message", role="assistant", model="claude-sonnet-5-5",
                          content=[block], stop_reason="tool_use", stop_sequence=None,
                          usage=SdkUsage(input_tokens=1, output_tokens=1))  # fmt: skip


async def test_anthropic_wire_name_of_an_unoffered_tool_raises_tool_not_allowed(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    """Pairs with the provider passing an unknown wire name through (names.get(b.name, b.name))."""
    client = StubAnthropic("notify__send")
    provider = AnthropicProvider("claude-sonnet-5-5", client=client)  # type: ignore[arg-type]
    with pytest.raises(ToolNotAllowed):
        await Runner(provider, registry, store).run("r1", protocol)
    assert invoked == []
    await assert_failed_closed(store, ToolNotAllowed, cursor=0)


async def test_gated_write_without_approval_raises_gate_required(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    with pytest.raises(GateRequired):
        await run(store, registry, protocol, [*LOOK_UP, *SUBMITTED])
    assert invoked == ["hris.get_balance"]
    await assert_failed_closed(store, GateRequired, cursor=1)


async def test_an_approval_authorises_one_call(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    await approve(store)
    again = scripted("", call("hris.submit_leave", **SUBMIT))
    with pytest.raises(GateRequired):
        await run(store, registry, protocol, [*LOOK_UP, again, again])
    assert invoked == ["hris.get_balance", "hris.submit_leave"]
    assert (await store.get_run("r1")).context["approvals_used"] == ["a1"]


async def test_context_accumulates_step_outputs(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol
) -> None:
    await approve(store)
    provider = await run(store, registry, protocol, [*LOOK_UP, *SUBMITTED, *NOTIFIED])
    stored = await store.get_run("r1")
    assert (stored.status, stored.cursor) == (RunStatus.completed, 3)
    steps = stored.context["steps"]
    assert [steps[n]["output"] for n in ("1", "2", "3")] == [
        "25 days left",
        "submitted",
        "told alice",
    ]
    assert steps["2"]["tool_calls"][0]["result"] == {"tool": "hris.submit_leave", "ok": True}
    # Step 2 sees the previous step by default; step 3 declares steps 1 and 2.
    first_plan_of = {c.step.number: c.context for c in reversed(provider.calls)}
    headings = {n: [m.content.split("\n")[0] for m in ms] for n, ms in first_plan_of.items()}
    one, two = f"Step 1 output {DATA}:", f"Step 2 output {DATA}:"
    assert headings == {1: [], 2: [one], 3: [one, two]}
    assert "25 days left" in first_plan_of[2][0].content


async def test_cursor_advances_only_after_a_step_completes(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    await approve(store)
    half_done = [*LOOK_UP, scripted("", call("hris.submit_leave", **SUBMIT))]
    with pytest.raises(ProviderError):  # the script runs out inside step 2
        await run(store, registry, protocol, half_done)
    assert invoked == ["hris.get_balance", "hris.submit_leave"]
    stored = await store.get_run("r1")
    assert list(stored.context["steps"]) == ["1"]
    await assert_failed_closed(store, ProviderError, cursor=1)


async def test_injected_provider_is_offered_only_the_step_tools(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol
) -> None:
    await approve(store)
    provider = await run(store, registry, protocol, [*LOOK_UP, *SUBMITTED, *NOTIFIED])
    offered = [(c.step.number, [t.name for t in c.tools]) for c in provider.calls]
    assert offered == [
        (1, ["hris.get_balance"]),
        (1, ["hris.get_balance"]),
        (2, ["hris.submit_leave"]),
        (2, ["hris.submit_leave"]),
        (3, ["notify.send"]),
        (3, ["notify.send"]),
    ]


async def test_a_run_starts_at_the_step_after_its_cursor(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol
) -> None:
    await store.update_run((await store.get_run("r1")).model_copy(update={"cursor": 2}))
    provider = await run(store, registry, protocol, NOTIFIED)
    assert {c.step.number for c in provider.calls} == {3}
    assert (await store.get_run("r1")).cursor == 3


async def test_a_step_stops_at_max_turns(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    looping = [scripted("", call("hris.get_balance"))] * 3
    with pytest.raises(LoopBudgetExceeded):
        await run(store, registry, protocol, looping)
    assert invoked == ["hris.get_balance"] * 2
    await assert_failed_closed(store, LoopBudgetExceeded, cursor=0)


def test_the_runner_is_the_only_caller_of_registry_invoke() -> None:
    """ToolRegistry.invoke checks no whitelist or gate, so only the runner may reach it.

    A static scan for the attribute name: a best-effort guard that a reviewer backs up, since
    getattr with a string would slip past it.
    """
    users = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                for node in ast.walk(func):
                    if isinstance(node, ast.Attribute) and node.attr == "invoke":
                        users.add((path.relative_to(SRC).as_posix(), func.name))
    # registry.py's own use is the Tool's Invoke inside ToolRegistry.invoke.
    assert users == {("registry.py", "invoke"), ("runner.py", "_act")}
