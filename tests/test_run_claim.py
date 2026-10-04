"""Issue #104: of two executions of one Run, from `run` and `resume` or from two `run`s, exactly
one goes on. Against InMemoryRunStore and, when DATABASE_URL is set, Postgres."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from cograil.domain import (
    Approval,
    AuditEvent,
    Colleague,
    Principal,
    Run,
    RunStatus,
    Step,
    Tool,
    Trigger,
)
from cograil.errors import GateRequired, ProviderError, RunClaimLost
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, Plan, PlannedToolCall, scripted
from cograil.providers.base import Message
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore, PostgresRunStore, RunStore

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
CONTACT = "hr-ops@example.com"
HARPER = Colleague(name="harper", role="HR", escalation_contact=CONTACT, protocols=["demo"])
PROTOCOL = parse_protocol("""
Protocol: demo
1. Step "Submit": Use @hris.submit_leave with the confirmed dates.
2. Step "Notify": Use @notify.send to tell the requester.
""")
TOOLS = [
    Tool(name="hris.submit_leave", kind="python", scope="write", confirm_before_write=True),
    Tool(name="notify.send", kind="python", scope="write", confirm_before_write=False),
]
SUBMIT = scripted("submit", PlannedToolCall(id="c1", tool="hris.submit_leave", args={}), done=True)
NOTIFY = [
    scripted("", PlannedToolCall(id="c2", tool="notify.send", args={})),
    scripted("told", done=True),
]


class InterleavingStore(InMemoryRunStore):
    """Yields to the event loop after every read of a Run, as a database round trip does."""

    async def get_run(self, run_id: str) -> Run:
        run = await super().get_run(run_id)
        await asyncio.sleep(0)
        return run


class TakeoverAtFailStore(InMemoryRunStore):
    """Another execution takes the Run over at the save that would fail it, between the
    failing execution's read of the Run and its save (#147)."""

    async def update_run(self, run: Run) -> None:
        if run.status is RunStatus.failed:
            stored = await self.get_run(run.id)
            await super().claim_run(
                stored.model_copy(update={"claim": "winner", "status": RunStatus.running}), stored
            )
        await super().update_run(run)


class TakeoverAtApprovalStore(InMemoryRunStore):
    """Another execution takes the Run over just before this one creates or spends an
    Approval, as `at` says (#143)."""

    def __init__(self, at: str) -> None:
        super().__init__()
        self.at = at

    async def _take_over(self, run_id: str) -> None:
        stored = await self.get_run(run_id)
        await self.claim_run(stored.model_copy(update={"claim": "winner"}), stored)

    async def create_approval(
        self, approval: Approval, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> None:
        if self.at == "create":
            await self._take_over(run.id)
        await super().create_approval(approval, run=run, events=events)

    async def spend_approval(
        self, token: str, spent_at: datetime, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> Approval:
        if self.at == "spend":
            await self._take_over(run.id)
        return await super().spend_approval(token, spent_at, run=run, events=events)


class HeldProvider(FakeProvider):
    """Sets `waiting`, then plans only once `release` is set."""

    def __init__(self, script: Sequence[Plan]) -> None:
        super().__init__(script)
        self.waiting, self.release = asyncio.Event(), asyncio.Event()

    async def plan(
        self, step: Step, context: Sequence[Message], tools: Sequence[Tool], **kwargs: Any
    ) -> Plan:
        self.waiting.set()
        await self.release.wait()
        return await super().plan(step, context, tools, **kwargs)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[RunStore]:
    if request.param == "memory":
        yield InterleavingStore()
        return
    pg_store = PostgresRunStore.from_url(request.getfixturevalue("migrated_url"))
    yield pg_store
    await pg_store.dispose()


@pytest.fixture
async def run_id(store: RunStore) -> str:
    run = Run(id=f"run-{uuid.uuid4().hex}", workspace="example-smb", colleague="harper",
              protocol="demo", protocol_version=1, principal=Principal(id="alice@example.com"),
              trigger=Trigger(kind="chat"), created_at=T0, updated_at=T0)  # fmt: skip
    await store.create_run(run)
    return run.id


@pytest.fixture
def invoked() -> list[str]:
    return []


@pytest.fixture
def make(store: RunStore, invoked: list[str]) -> Any:
    registry = ToolRegistry(store)
    for tool in TOOLS:

        async def invoke(args: dict[str, Any], name: str = tool.name) -> dict[str, Any]:
            invoked.append(name)
            return {"ok": True}

        registry.register(tool, invoke)

    def make(provider: FakeProvider) -> Runner:
        return Runner(provider, registry, store, HARPER)

    return make


async def kinds(store: RunStore, run_id: str) -> list[str]:
    return [e.kind for e in await store.list_audit_events(run_id)]


async def test_two_concurrent_runs_let_exactly_one_proceed(
    store: RunStore, run_id: str, make: Any
) -> None:
    results = await asyncio.gather(
        make(FakeProvider([SUBMIT])).run(run_id, PROTOCOL),
        make(FakeProvider([SUBMIT])).run(run_id, PROTOCOL),
        return_exceptions=True,
    )
    went_on = [r for r in results if isinstance(r, Run)]
    assert [r.status for r in went_on] == [RunStatus.awaiting_approval]  # paused at the gate
    stopped = [r for r in results if not isinstance(r, Run)]
    assert len(stopped) == 1 and isinstance(stopped[0], RunClaimLost | GateRequired)
    assert (await store.get_run(run_id)).status is RunStatus.awaiting_approval
    assert "run.failed" not in await kinds(store, run_id)


async def test_a_run_racing_a_resume_lets_exactly_one_proceed(
    store: RunStore, run_id: str, make: Any, invoked: list[str]
) -> None:
    """The race of issue #104: `run` on a Run that `resume` has just set running."""
    await make(FakeProvider([SUBMIT])).run(run_id, PROTOCOL)
    (approval,) = await store.list_approvals(run_id)
    held = HeldProvider([scripted("told", done=True)])
    resuming = asyncio.create_task(make(held).resume(approval.token, PROTOCOL, decider=CONTACT))
    await held.waiting.wait()  # resumed: Step 1 is done and Step 2 is planning

    ran = await make(FakeProvider(NOTIFY)).run(run_id, PROTOCOL)
    held.release.set()
    with pytest.raises(RunClaimLost):
        await resuming

    assert ran.status is RunStatus.completed
    stored = await store.get_run(run_id)
    assert (stored.status, stored.claim) == (RunStatus.completed, ran.claim)
    assert invoked == ["hris.submit_leave", "notify.send"]
    events = await kinds(store, run_id)
    assert events.count("run.completed") == 1
    assert "run.failed" not in events


async def test_a_failure_is_the_callers_to_see_when_the_run_is_taken_over_as_it_fails() -> None:
    """Issue #147: a Run taken over between the failing read and the save is the winner's to
    fail, so the caller sees the real error, not RunClaimLost, and no run.failed is written."""
    store = TakeoverAtFailStore()  # memory only: the race is the runner's to answer
    run = Run(id=f"run-{uuid.uuid4().hex}", workspace="example-smb", colleague="harper",
              protocol="demo", protocol_version=1, principal=Principal(id="alice@example.com"),
              trigger=Trigger(kind="chat"), created_at=T0, updated_at=T0)  # fmt: skip
    await store.create_run(run)
    registry = ToolRegistry(store)
    for tool in TOOLS:

        async def invoke(args: dict[str, Any], name: str = tool.name) -> dict[str, Any]:
            return {"ok": True}

        registry.register(tool, invoke)

    with pytest.raises(ProviderError):  # the empty script runs out at the first plan
        await Runner(FakeProvider([]), registry, store, HARPER).run(run.id, PROTOCOL)
    stored = await store.get_run(run.id)
    assert (stored.status, stored.claim) == (RunStatus.running, "winner")
    assert "run.failed" not in await kinds(store, run.id)


@pytest.mark.parametrize("at", ["create", "spend"])
async def test_an_execution_that_lost_its_claim_neither_pauses_nor_spends(at: str) -> None:
    """Issue #143: taken over at the gate, an execution leaves no pending Approval behind,
    spends none and runs no gated write; the Run stays the winner's."""
    store = TakeoverAtApprovalStore(at)  # memory only: test_store covers Postgres
    run = Run(id=f"run-{uuid.uuid4().hex}", workspace="example-smb", colleague="harper",
              protocol="demo", protocol_version=1, principal=Principal(id="alice@example.com"),
              trigger=Trigger(kind="chat"), created_at=T0, updated_at=T0)  # fmt: skip
    await store.create_run(run)
    seeded: list[Approval] = []
    if at == "spend":  # an approved Approval this execution finds and goes to spend
        seeded.append(Approval(token="a1", run_id=run.id, step=1, tool="hris.submit_leave",
                               args={}, approver=CONTACT, decision="approved"))  # fmt: skip
        await store.create_approval(seeded[0], run=run)
    invoked: list[str] = []
    registry = ToolRegistry(store)
    for tool in TOOLS:

        async def invoke(args: dict[str, Any], name: str = tool.name) -> dict[str, Any]:
            invoked.append(name)
            return {"ok": True}

        registry.register(tool, invoke)

    with pytest.raises(RunClaimLost):
        await Runner(FakeProvider([SUBMIT]), registry, store, HARPER).run(run.id, PROTOCOL)
    assert invoked == []
    assert await store.list_approvals(run.id) == seeded  # none created, none spent
    stored = await store.get_run(run.id)
    assert (stored.status, stored.claim) == (RunStatus.running, "winner")
    assert not {"gate.paused", "gate.spent", "run.failed"} & set(await kinds(store, run.id))
