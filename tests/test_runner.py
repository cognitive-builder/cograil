"""Runner tests for issue #10: whitelist, gates, context, cursor, bounds, invoke's only caller.

Pausing, resuming and escalating at a gate are in test_gates.py (issue #11). The bounded
step loop, step_complete and the harness version stamp are issue #44; the tool pack version
stamp is #110.
"""

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import ToolUseBlock
from anthropic.types import Usage as SdkUsage

from cograil.domain import (
    Approval,
    Colleague,
    Harness,
    LoopBounds,
    Price,
    Protocol,
    RunStatus,
    Tiers,
    Tool,
)
from cograil.errors import ProviderError, ToolNotAllowed
from cograil.harness import harness_version
from cograil.parser import parse_protocol
from cograil.providers import (
    AnthropicProvider,
    FakeProvider,
    Message,
    Plan,
    PlannedToolCall,
    scripted,
)
from cograil.registry import ToolRegistry
from cograil.runner import NOT_COMPLETE, Runner
from cograil.store import InMemoryRunStore

SRC = Path(__file__).parents[1] / "src/cograil"
SUBMIT = {"employee": "alice", "days": 3}
HARPER = Colleague(
    name="harper", role="HR", escalation_contact="hr-ops@example.com", protocols=["demo"]
)
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


LOOK_UP = [scripted("", call("hris.get_balance")), scripted("25 days left", done=True)]
SUBMITTED = [scripted("", call("hris.submit_leave", **SUBMIT)), scripted("submitted", done=True)]
NOTIFIED = [scripted("", call("notify.send")), scripted("told alice", done=True)]


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
                 approver="bob", decision="approved"),
        run=await store.get_run("r1"),
    )  # fmt: skip


async def run(
    store: InMemoryRunStore,
    registry: ToolRegistry,
    protocol: Protocol,
    script: list[Plan],
    harness: Harness | None = None,
) -> FakeProvider:
    provider = FakeProvider(script)
    await Runner(provider, registry, store, HARPER, harness=harness).run("r1", protocol)
    return provider


async def start_at(store: InMemoryRunStore, cursor: int) -> None:
    await store.update_run((await store.get_run("r1")).model_copy(update={"cursor": cursor}))


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
@pytest.mark.parametrize("done", [False, True], ids=["", "with-step-complete"])
async def test_off_whitelist_call_raises_tool_not_allowed_and_fails_closed(
    store: InMemoryRunStore,
    registry: ToolRegistry,
    protocol: Protocol,
    invoked: list[str],
    planned: list[PlannedToolCall],
    done: bool,
) -> None:
    await approve(store)
    with pytest.raises(ToolNotAllowed):
        await run(store, registry, protocol, [scripted("", *planned, done=done)])
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
        await Runner(provider, registry, store, HARPER).run("r1", protocol)
    assert invoked == []
    await assert_failed_closed(store, ToolNotAllowed, cursor=0)


async def test_an_approval_authorises_one_call(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    await approve(store)
    again = scripted("", call("hris.submit_leave", **SUBMIT))
    await run(store, registry, protocol, [*LOOK_UP, again, again])
    assert invoked == ["hris.get_balance", "hris.submit_leave"]
    assert (await store.get_approval("a1")).spent_at is not None
    assert (await store.get_run("r1")).status == RunStatus.awaiting_approval  # the second call


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
    seen = {n: re.findall(r'<data source="(.+?)">', "".join(m.content for m in ms))
            for n, ms in first_plan_of.items()}  # fmt: skip
    assert seen == {1: [], 2: ["step 1"], 3: ["step 1", "step 2"]}
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
    await start_at(store, 2)
    provider = await run(store, registry, protocol, NOTIFIED)
    assert {c.step.number for c in provider.calls} == {3}
    assert (await store.get_run("r1")).cursor == 3


async def assert_escalated_at(
    store: InMemoryRunStore, bound: str, limit: float, cursor: int
) -> None:
    """The Run escalated through the gates with a loop.bounded AuditEvent, and did not fail."""
    stored = await store.get_run("r1")
    assert (stored.status, stored.cursor) == (RunStatus.escalated, cursor)
    events = await store.list_audit_events("r1")
    bounded, escalated = events[-2:]
    assert (bounded.kind, bounded.detail["bound"], bounded.detail["limit"]) == (
        "loop.bounded", bound, limit
    )  # fmt: skip
    assert (escalated.kind, escalated.principal_id) == ("run.escalated", "alice@example.com")
    assert (escalated.detail["reason"], escalated.detail["contact"]) == (
        "loop_budget_exceeded", HARPER.escalation_contact
    )  # fmt: skip
    assert "run.failed" not in {e.kind for e in events}


@pytest.mark.parametrize(
    ("default_turns", "cursor", "tool", "turns"),
    [
        pytest.param(6, 0, "hris.get_balance", 2, id="step-turns-under-the-default"),
        pytest.param(1, 0, "hris.get_balance", 2, id="step-turns-over-the-default"),
        pytest.param(1, 2, "notify.send", 1, id="harness-default"),
    ],
)
async def test_a_step_stops_at_max_turns_and_escalates(
    store: InMemoryRunStore,
    registry: ToolRegistry,
    protocol: Protocol,
    invoked: list[str],
    default_turns: int,
    cursor: int,
    tool: str,
    turns: int,
) -> None:
    """Step 1 declares `(turns: 2)`, which wins over the harness default; step 3 does not."""
    await start_at(store, cursor)
    harness = Harness(loop=LoopBounds(max_turns=default_turns))
    provider = await run(store, registry, protocol, [scripted("", call(tool))] * 3, harness)
    assert len(provider.calls) == turns
    assert invoked == [tool] * turns
    await assert_escalated_at(store, "max_turns", turns, cursor)


def priced(model: str, usd_per_mtok: float) -> dict[str, Any]:
    """Harness fields that price `model`, every tier's model, at usd_per_mtok in and out."""
    return {
        "tiers": Tiers(small=model, standard=model, strong=model),
        "pricing": {model: Price(input_per_mtok=usd_per_mtok, output_per_mtok=usd_per_mtok)},
    }


FAKE_PRICED, UNPRICED = priced("fake-model", 1000), priced("other-model", 1)


@pytest.mark.parametrize(
    ("harness", "bound", "limit", "calls", "cost"),
    [
        pytest.param(Harness(loop=LoopBounds(token_budget_per_step=20)),
                     "token_budget_per_step", 20, 2, 0.0, id="tokens"),
        pytest.param(Harness(loop=LoopBounds(usd_budget_per_run=0.02), **FAKE_PRICED),
                     "usd_budget_per_run", 0.02, 2, 0.03, id="dollars"),
        pytest.param(Harness(loop=LoopBounds(usd_budget_per_run=0.02), **UNPRICED),
                     "usd_budget_per_run", 0.02, 1, 0.0, id="unpriced-model"),
    ],
)  # fmt: skip
async def test_a_budget_breach_escalates(
    store: InMemoryRunStore,
    registry: ToolRegistry,
    protocol: Protocol,
    harness: Harness,
    bound: str,
    limit: float,
    calls: int,
    cost: float,
) -> None:
    """Each scripted call spends 15 tokens; priced at 1000 USD per million, 0.015 USD."""
    await start_at(store, 2)
    looping = [scripted("", call("notify.send"))] * 3
    provider = await run(store, registry, protocol, looping, harness)
    assert len(provider.calls) == calls
    await assert_escalated_at(store, bound, limit, cursor=2)
    assert (await store.get_run("r1")).cost_usd == pytest.approx(cost)


async def test_a_step_ends_only_on_step_complete(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol, invoked: list[str]
) -> None:
    """A plain answer is not the end; a plan with calls and step_complete runs the calls first."""
    await start_at(store, 2)
    script = [scripted("all done"), scripted("told alice", call("notify.send"), done=True)]
    provider = await run(store, registry, protocol, script)
    assert provider.calls[1].context[-2:] == [
        Message(role="assistant", content="all done"),
        Message(role="user", content=NOT_COMPLETE),
    ]
    assert invoked == ["notify.send"]
    stored = await store.get_run("r1")
    assert (stored.status, stored.cursor) == (RunStatus.completed, 3)
    record = stored.context["steps"]["3"]
    assert (record["output"], len(record["tool_calls"])) == ("told alice", 1)


async def test_the_harness_and_tool_pack_versions_are_stamped_on_the_run(
    store: InMemoryRunStore, registry: ToolRegistry, protocol: Protocol
) -> None:
    await start_at(store, 2)
    harness = Harness(version="1.2.3", loop=LoopBounds(max_turns=4))
    registry.tool_pack_version = "pack-1"
    await run(store, registry, protocol, NOTIFIED, harness)
    stored = await store.get_run("r1")
    assert stored.harness_version == harness_version(harness)
    assert stored.harness_version.startswith("1.2.3+")
    assert stored.tool_pack_version == "pack-1"
    started = (await store.list_audit_events("r1"))[0]
    assert started.detail["harness_version"] == stored.harness_version
    assert started.detail["tool_pack_version"] == "pack-1"


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
    assert users == {("registry.py", "invoke"), ("runner.py", "_invoke")}
