"""Gate tests for issue #11: pause, resume, escalate, failure thresholds and GateRequired.

The approval timeout comes from the Harness (issue #44).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cograil.context import DATA_PREAMBLE
from cograil.domain import (
    Approval,
    ApprovalSettings,
    Colleague,
    Harness,
    Protocol,
    RunStatus,
    Tool,
)
from cograil.errors import (
    ApprovalAlreadyDecided,
    GateRequired,
    RunNotPaused,
    ToolExecutionError,
)
from cograil.gates import require_approval
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, Plan, PlannedToolCall, scripted
from cograil.registry import CallContext, ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
TIMEOUT = timedelta(hours=1)
HARNESS = Harness(approvals=ApprovalSettings(timeout_hours=1))
CONTACT = "hr-ops@example.com"
HARPER = Colleague(name="harper", role="HR", escalation_contact=CONTACT, protocols=["demo"])
SUBMIT = {"employee": "alice", "days": 3}
OTHER = {"employee": "alice", "days": 1}
RULE = "@hris.get_balance fails: retry once, then escalate to the Human Manager."
PROTOCOL = f"""
Protocol: demo
1. Step "Look up": Use @hris.get_balance for the requester.
2. Step "Submit": Use @hris.submit_leave with the confirmed dates.
3. Step "Notify": Use @notify.send to tell the requester.

Error handling:
- {RULE}
"""
TOOLS = [
    Tool(name="hris.get_balance", kind="python", scope="read"),
    Tool(name="hris.submit_leave", kind="python", scope="write", confirm_before_write=True),
    Tool(name="notify.send", kind="python", scope="write", confirm_before_write=False),
]


def call(tool: str, **args: Any) -> PlannedToolCall:
    return PlannedToolCall(id=f"call-{tool}", tool=tool, args=args)


LOOK_UP = [scripted("", call("hris.get_balance")), scripted("25 days left", done=True)]
TO_GATE = [*LOOK_UP, scripted("", call("hris.submit_leave", **SUBMIT))]
AFTER_GATE = [
    scripted("submitted", done=True),
    scripted("", call("notify.send")),
    scripted("told alice", done=True),
]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Tools:
    """The registry's invokes: each records its name; a tool in `failing` raises N times."""

    def __init__(self) -> None:
        self.invoked: list[str] = []
        self.failing: dict[str, int] = {}

    async def invoke(self, name: str) -> dict[str, Any]:
        self.invoked.append(name)
        if self.failing.get(name, 0) > 0:
            self.failing[name] -= 1
            raise RuntimeError("HRIS is down")
        return {"tool": name, "ok": True}


@pytest.fixture
def protocol() -> Protocol:
    return parse_protocol(PROTOCOL)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tools() -> Tools:
    return Tools()


def build_registry(store: InMemoryRunStore, tools: Tools) -> ToolRegistry:
    registry = ToolRegistry(store)
    for tool in TOOLS:

        async def invoke(args: dict[str, Any], name: str = tool.name) -> dict[str, Any]:
            return await tools.invoke(name)

        registry.register(tool, invoke)
    return registry


@pytest.fixture
def registry(store: InMemoryRunStore, tools: Tools) -> ToolRegistry:
    return build_registry(store, tools)


@pytest.fixture
def make(store: InMemoryRunStore, registry: ToolRegistry, clock: Clock) -> Any:
    def make(script: list[Plan]) -> tuple[Runner, FakeProvider]:
        provider = FakeProvider(script)
        runner = Runner(provider, registry, store, HARPER, harness=HARNESS, clock=clock)
        return runner, provider

    return make


async def pause(make: Any, protocol: Protocol, store: InMemoryRunStore) -> str:
    runner, _ = make(TO_GATE)
    await runner.run("r1", protocol)
    (approval,) = await store.list_approvals("r1")
    return str(approval.token)


async def kinds(store: InMemoryRunStore) -> list[str]:
    return [e.kind for e in await store.list_audit_events("r1")]


async def test_gated_write_pauses_the_run_awaiting_approval_and_persists_state(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    token = await pause(make, protocol, store)
    stored = await store.get_run("r1")
    assert (stored.status, stored.cursor) == (RunStatus.awaiting_approval, 1)
    approval = await store.get_approval(token)
    assert approval.model_dump(exclude={"token"}) == {
        "run_id": "r1", "step": 2, "tool": "hris.submit_leave", "args": SUBMIT,
        "approver": "alice@example.com", "decision": "pending", "decided_at": None,
        "expires_at": T0 + TIMEOUT, "spent_at": None,
    }  # fmt: skip
    paused = stored.context["paused"]
    assert (paused["token"], paused["step"], paused["turn"]) == (token, 2, 1)
    assert paused["planned"] == [call("hris.submit_leave", **SUBMIT).model_dump()]
    assert tools.invoked == ["hris.get_balance"]
    last = (await store.list_audit_events("r1"))[-1]
    assert (last.kind, last.principal_id) == ("gate.paused", "alice@example.com")
    assert last.detail["token"] == token
    assert "run.failed" not in await kinds(store)


async def test_resume_continues_exactly_at_the_paused_step(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    token = await pause(make, protocol, store)
    runner, provider = make(AFTER_GATE)
    run = await runner.resume(token, protocol)
    assert (run.status, run.cursor) == (RunStatus.completed, 3)
    assert tools.invoked == ["hris.get_balance", "hris.submit_leave", "notify.send"]
    # The saved plan ran without asking the model again; its first turn after the gate is
    # step 2's second, which sees step 1's output and the submit result.
    first = provider.calls[0]
    assert first.step.number == 2
    assert first.context[0].content.startswith(DATA_PREAMBLE)
    assert first.context[-1].content.startswith(DATA_PREAMBLE)
    assert "hris.submit_leave" in first.context[-1].content
    assert "paused" not in run.context
    approval = await store.get_approval(token)
    assert (approval.decision, approval.spent_at) == ("approved", T0)
    seen = await kinds(store)
    assert seen[seen.index("gate.paused") :][:3] == ["gate.paused", "gate.resumed", "gate.spent"]
    assert seen[-1] == "run.completed"


async def test_a_plan_that_ends_its_step_at_a_gate_ends_it_after_the_resume(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    """step_complete beside a gated call is saved with the paused plan (issue #44)."""
    ending = scripted("submitted", call("hris.submit_leave", **SUBMIT), done=True)
    runner, _ = make([*LOOK_UP, ending])
    await runner.run("r1", protocol)
    (approval,) = await store.list_approvals("r1")
    runner, provider = make(AFTER_GATE[1:])
    run = await runner.resume(approval.token, protocol)
    assert (run.status, run.cursor) == (RunStatus.completed, 3)
    assert {c.step.number for c in provider.calls} == {3}  # step 2 ended without another turn
    assert run.context["steps"]["2"]["output"] == "submitted"
    assert tools.invoked == ["hris.get_balance", "hris.submit_leave", "notify.send"]


async def test_racing_resumes_let_one_through(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    token = await pause(make, protocol, store)
    (one, _), (two, _) = make(AFTER_GATE), make(AFTER_GATE)
    results = await asyncio.gather(
        one.resume(token, protocol), two.resume(token, protocol), return_exceptions=True
    )
    done = [r for r in results if not isinstance(r, BaseException)]
    lost = [r for r in results if isinstance(r, BaseException)]
    assert [r.status for r in done] == [RunStatus.completed]
    assert len(lost) == 1 and isinstance(lost[0], ApprovalAlreadyDecided | RunNotPaused)
    assert tools.invoked.count("hris.submit_leave") == 1


@pytest.mark.parametrize(
    ("how", "late", "decision", "reason"),
    [
        pytest.param("decline", False, "declined", "approval_declined", id="declined"),
        pytest.param("approve", True, "expired", "approval_expired", id="approved-too-late"),
        pytest.param("expire", True, "expired", "approval_expired", id="timed-out"),
    ],
)
async def test_declined_or_timed_out_approval_escalates_to_the_escalation_contact(
    store: InMemoryRunStore,
    make: Any,
    protocol: Protocol,
    tools: Tools,
    clock: Clock,
    how: str,
    late: bool,
    decision: str,
    reason: str,
) -> None:
    token = await pause(make, protocol, store)
    clock.now = T0 + TIMEOUT if late else T0
    runner, provider = make(AFTER_GATE)
    if how == "expire":
        run = await runner.expire(token)
    else:
        run = await runner.resume(token, protocol, "declined" if how == "decline" else "approved")
    assert run.status == RunStatus.escalated
    assert (await store.get_run("r1")).status == RunStatus.escalated
    assert (await store.get_approval(token)).decision == decision
    assert provider.calls == []
    assert tools.invoked == ["hris.get_balance"]
    last = (await store.list_audit_events("r1"))[-1]
    assert (last.kind, last.principal_id) == ("run.escalated", "alice@example.com")
    assert last.detail == {"reason": reason, "contact": CONTACT, "step": 2,
                           "tool": "hris.submit_leave", "token": token}  # fmt: skip


async def test_expire_before_the_deadline_changes_nothing(
    store: InMemoryRunStore, make: Any, protocol: Protocol, clock: Clock
) -> None:
    token = await pause(make, protocol, store)
    clock.now = T0 + TIMEOUT - timedelta(seconds=1)
    runner, _ = make([])
    assert (await runner.expire(token)).status == RunStatus.awaiting_approval
    assert (await store.get_approval(token)).decision == "pending"


async def test_failure_threshold_stops_the_run_and_escalates(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    tools.failing["hris.get_balance"] = 2
    again = scripted("", call("hris.get_balance"))
    runner, provider = make([again, again, scripted("never asked")])
    run = await runner.run("r1", protocol)
    assert (run.status, run.cursor) == (RunStatus.escalated, 0)
    assert len(provider.calls) == 2  # the first failure went back to the model as data
    assert "HRIS is down" in provider.calls[1].context[-1].content
    last = (await store.list_audit_events("r1"))[-1]
    assert (last.kind, last.principal_id) == ("run.escalated", "alice@example.com")
    assert {k: last.detail[k] for k in ("reason", "contact", "tool", "failures", "rule")} == {
        "reason": "failure_threshold", "contact": CONTACT, "tool": "hris.get_balance",
        "failures": 2, "rule": RULE,
    }  # fmt: skip


@pytest.mark.parametrize("done", [False, True], ids=["", "step-complete-beside-it"])
async def test_a_failure_under_the_threshold_lets_the_step_retry(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools, done: bool
) -> None:
    """A step_complete planned beside the failed call does not end the Step (issue #44)."""
    tools.failing["hris.get_balance"] = 1
    first = scripted("guessed", call("hris.get_balance"), done=done)
    runner, _ = make([first, *LOOK_UP, *TO_GATE[2:]])
    run = await runner.run("r1", protocol)
    assert (run.status, run.cursor) == (RunStatus.awaiting_approval, 1)
    assert run.context["failures"] == {"hris.get_balance": 1}
    assert run.context["steps"]["1"]["output"] == "25 days left"


async def test_a_failure_of_a_tool_without_a_threshold_fails_the_run(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    tools.failing["notify.send"] = 1
    await store.update_run((await store.get_run("r1")).model_copy(update={"cursor": 2}))
    runner, _ = make([scripted("", call("notify.send"))])
    with pytest.raises(ToolExecutionError):
        await runner.run("r1", protocol)
    assert (await store.get_run("r1")).status == RunStatus.failed


async def test_nothing_runs_until_every_gated_call_of_a_plan_is_approved(
    store: InMemoryRunStore, make: Any, protocol: Protocol, tools: Tools
) -> None:
    both = scripted("", call("hris.submit_leave", **SUBMIT), call("hris.submit_leave", **OTHER))
    runner, _ = make([*LOOK_UP, both])
    await runner.run("r1", protocol)
    (first,) = await store.list_approvals("r1")
    runner, provider = make([])
    run = await runner.resume(first.token, protocol)
    assert run.status == RunStatus.awaiting_approval
    assert provider.calls == []
    assert tools.invoked == ["hris.get_balance"]
    second = next(a for a in await store.list_approvals("r1") if a.token != first.token)
    assert (second.args, (await store.get_approval(first.token)).spent_at) == (OTHER, None)
    runner, _ = make(AFTER_GATE)
    assert (await runner.resume(second.token, protocol)).status == RunStatus.completed
    assert tools.invoked.count("hris.submit_leave") == 2


def approval(**changes: Any) -> Approval:
    fields = {"token": "a1", "run_id": "r1", "step": 2, "tool": "hris.submit_leave",
              "args": SUBMIT, "approver": "bob", "decision": "approved"}  # fmt: skip
    return Approval(**{**fields, **changes})


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(None, id="none"),
        pytest.param(approval(decision="pending"), id="pending"),
        pytest.param(approval(decision="declined"), id="declined"),
        pytest.param(approval(args=OTHER), id="other-args"),
        pytest.param(approval(step=3), id="other-step"),
        pytest.param(approval(spent_at=T0), id="spent"),
    ],
)
async def test_require_approval_raises_gate_required_without_an_approved_unspent_match(
    store: InMemoryRunStore, ctx: CallContext, existing: Approval | None
) -> None:
    if existing is not None:
        await store.create_approval(existing)
    with pytest.raises(GateRequired):
        await require_approval(store, ctx, TOOLS[1], SUBMIT, claimed=[])


async def test_require_approval_returns_a_match_once_per_plan(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    await store.create_approval(approval())
    found = await require_approval(store, ctx, TOOLS[1], SUBMIT, claimed=[])
    assert found is not None and found.token == "a1"
    with pytest.raises(GateRequired):
        await require_approval(store, ctx, TOOLS[1], SUBMIT, claimed=["a1"])
    assert await require_approval(store, ctx, TOOLS[2], {}, claimed=[]) is None  # ungated


async def test_run_of_a_paused_run_raises_gate_required_and_keeps_it_paused(
    store: InMemoryRunStore, make: Any, protocol: Protocol
) -> None:
    await pause(make, protocol, store)
    runner, provider = make(AFTER_GATE)
    with pytest.raises(GateRequired):
        await runner.run("r1", protocol)
    assert (await store.get_run("r1")).status == RunStatus.awaiting_approval
    assert provider.calls == []


class RacedStore(InMemoryRunStore):
    """Another resume spends each Approval just before this one tries to."""

    async def spend_approval(self, token: str, spent_at: datetime) -> Approval:
        await super().spend_approval(token, spent_at)
        return await super().spend_approval(token, spent_at)


async def test_an_approval_spent_by_a_racing_call_raises_gate_required(
    store: InMemoryRunStore, protocol: Protocol, tools: Tools
) -> None:
    raced = RacedStore()
    await raced.create_run(await store.get_run("r1"))
    await raced.create_approval(approval())
    runner = Runner(FakeProvider(TO_GATE), build_registry(raced, tools), raced, HARPER)
    with pytest.raises(GateRequired):
        await runner.run("r1", protocol)
    assert tools.invoked == ["hris.get_balance"]
    assert (await raced.get_run("r1")).status == RunStatus.failed


async def test_a_token_the_run_is_not_paused_on_cannot_resume_it(
    store: InMemoryRunStore, make: Any, protocol: Protocol
) -> None:
    await store.create_approval(approval(decision="pending"))
    runner, _ = make([])
    with pytest.raises(RunNotPaused):
        await runner.resume("a1", protocol)
    assert (await store.get_approval("a1")).decision == "pending"
