"""RunStore contract, run against InMemoryRunStore and, when DATABASE_URL is set, Postgres."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import Approval, AuditEvent, Principal, Run, RunStatus, ToolCall, Trigger
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    DuplicateRecord,
    RunClaimLost,
    RunNotFound,
)
from cograil.store import InMemoryRunStore, PostgresRunStore, RunStore

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
async def pg_engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_url)
    yield engine
    await engine.dispose()


@pytest.fixture(
    params=["memory", pytest.param("postgres", marks=pytest.mark.integration)],
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[RunStore]:
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    # An async fixture cannot request another async fixture by name, so this one owns its engine.
    pg_store = PostgresRunStore.from_url(request.getfixturevalue("migrated_url"))
    yield pg_store
    await pg_store.dispose()


def make_run(run_id: str | None = None) -> Run:
    return Run(
        id=run_id or f"run-{uuid.uuid4().hex}",
        workspace="example-smb",
        colleague="hr",
        protocol="leave_request",
        protocol_version=1,
        harness_version="h1",
        principal=Principal(id="alice@example.com", groups=["staff"]),
        trigger=Trigger(kind="chat", channel="web"),
        context={"days": 3},
        created_at=T0,
        updated_at=T0,
    )


async def stored_run(store: RunStore) -> Run:
    run = make_run()
    await store.create_run(run)
    return run


def unique_principal(name: str) -> str:
    """A principal id no other test uses: Postgres rows outlive a test inside one CI job."""
    return f"{name}-{uuid.uuid4().hex}@example.com"


def run_for(principal_id: str) -> Run:
    return make_run().model_copy(
        update={"principal": Principal(id=principal_id), "principal_id": principal_id}
    )


async def test_run_round_trips_and_update_saves_progress(store: RunStore) -> None:
    run = await stored_run(store)
    assert await store.get_run(run.id) == run

    progressed = run.model_copy(
        update={
            "status": RunStatus.awaiting_approval,
            "cursor": 2,
            "context": {"days": 3, "approved": False},
            "cost_usd": 0.0125,
            "harness_version": "1.0.0+3f2a9c1b7d4e",
            "updated_at": T0 + timedelta(minutes=1),
        }
    )
    await store.update_run(progressed)
    assert await store.get_run(run.id) == progressed


async def test_list_runs_newest_first_up_to_the_limit(store: RunStore) -> None:
    ids = []
    for minutes in (0, 2, 1):
        run = make_run().model_copy(update={"created_at": T0 + timedelta(minutes=minutes)})
        await store.create_run(run)
        ids.append(run.id)
    listed = await store.list_runs(limit=2)
    assert [r.id for r in listed] == [ids[1], ids[2]]


async def test_list_runs_can_be_limited_to_one_principal(store: RunStore) -> None:
    alice, bob = unique_principal("alice"), unique_principal("bob")
    mine, other = run_for(alice), run_for(bob)
    await store.create_run(mine)
    await store.create_run(other)
    assert [r.id for r in await store.list_runs(principal_id=alice)] == [mine.id]
    assert [r.id for r in await store.list_runs(principal_id=bob)] == [other.id]
    assert await store.list_runs(principal_id=unique_principal("nobody")) == []


async def test_run_errors(store: RunStore) -> None:
    run = await stored_run(store)
    with pytest.raises(DuplicateRecord):
        await store.create_run(run)
    with pytest.raises(RunNotFound):
        await store.get_run("missing")
    with pytest.raises(RunNotFound):
        await store.update_run(make_run("missing"))


async def test_tool_calls_keep_order_and_values(store: RunStore) -> None:
    run = await stored_run(store)
    ok = ToolCall(
        step=0,
        tool="hris.get_balance",
        args={"employee": "alice"},
        result={"days": 12},
        started_at=T0,
        ended_at=T0 + timedelta(seconds=1),
    )
    failed = ToolCall(step=1, tool="hris.book", args={"days": 3}, error="denied", started_at=T0)
    await store.record_tool_call(run.id, ok)
    await store.record_tool_call(run.id, failed)

    assert await store.list_tool_calls(run.id) == [ok, failed]
    with pytest.raises(RunNotFound):
        await store.record_tool_call("missing", ok)


async def test_approval_is_decided_once(store: RunStore) -> None:
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    pending = Approval(
        token=token, run_id=run.id, step=1, tool="hris.book", args={"days": 3}, approver="bob"
    )
    await store.create_approval(pending)
    assert await store.get_approval(token) == pending
    assert await store.list_approvals(run.id) == [pending]

    decided = await store.decide_approval(token, "approved", T0, run=run)
    assert decided.decision == "approved"
    assert decided.decided_at == T0
    assert await store.get_approval(token) == decided

    with pytest.raises(ApprovalAlreadyDecided):
        await store.decide_approval(token, "declined", T0, run=run)
    assert (await store.get_approval(token)).decision == "approved"


async def test_deciding_an_approval_saves_its_run_with_it(store: RunStore) -> None:
    """Issue #95: the decision and the Run's save happen together, or neither does."""
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    await store.create_approval(
        Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={}, approver="bob")
    )
    paused = run.model_copy(update={"status": RunStatus.awaiting_approval})
    await store.update_run(paused)
    resumed = paused.model_copy(update={"status": RunStatus.running, "updated_at": T0})

    lost = resumed.model_copy(update={"id": "missing"})  # the save fails after the decision
    with pytest.raises(RunNotFound):
        await store.decide_approval(token, "approved", T0, run=lost)
    assert (await store.get_approval(token)).decision == "pending"
    assert await store.get_run(run.id) == paused

    decided = await store.decide_approval(token, "approved", T0, run=resumed)
    assert decided.decision == "approved"
    assert await store.get_run(run.id) == resumed


async def test_deciding_an_approval_writes_its_audit_events_with_it(store: RunStore) -> None:
    """Issue #104: the decision, the Run and its AuditEvents are saved together or not at all."""
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    await store.create_approval(
        Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={}, approver="bob")
    )
    resumed = run.model_copy(update={"status": RunStatus.running, "updated_at": T0})
    event = AuditEvent(
        run_id=run.id, at=T0, principal_id="bob", kind="gate.resumed", detail={"token": token}
    )

    unwritable = event.model_copy(update={"run_id": "missing"})  # fails the whole decision
    with pytest.raises(RunNotFound):
        await store.decide_approval(token, "approved", T0, run=resumed, events=[event, unwritable])
    stale = resumed.model_copy(update={"claim": "another execution"})
    with pytest.raises(RunClaimLost):
        await store.decide_approval(token, "approved", T0, run=stale, events=[event])
    assert (await store.get_approval(token)).decision == "pending"
    assert await store.get_run(run.id) == run
    assert await store.list_audit_events(run.id) == []

    await store.decide_approval(token, "approved", T0, run=resumed, events=[event])
    assert await store.get_run(run.id) == resumed
    assert await store.list_audit_events(run.id) == [event]


async def test_run_is_claimed_once_even_by_concurrent_claims(store: RunStore) -> None:
    """Issue #104: of two claims from one read one wins, and the other can no longer save."""
    run = await stored_run(store)
    claims = [run.model_copy(update={"status": RunStatus.running, "claim": c}) for c in "ab"]
    results = await asyncio.gather(
        *(store.claim_run(claim, run) for claim in claims), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["NoneType", "RunClaimLost"]
    winner, loser = claims if results[0] is None else reversed(claims)
    assert await store.get_run(run.id) == winner

    with pytest.raises(RunClaimLost):
        await store.update_run(loser.model_copy(update={"cursor": 1}))
    with pytest.raises(RunClaimLost):
        await store.claim_run(loser, run)
    progressed = winner.model_copy(update={"cursor": 1})
    await store.update_run(progressed)
    assert await store.get_run(run.id) == progressed

    paused = progressed.model_copy(update={"status": RunStatus.awaiting_approval})
    await store.update_run(paused)
    with pytest.raises(RunClaimLost):  # read running, but it paused before the claim
        await store.claim_run(loser.model_copy(update={"claim": "c"}), progressed)
    assert await store.get_run(run.id) == paused
    with pytest.raises(RunNotFound):
        await store.claim_run(make_run("missing"), make_run("missing"))


async def test_approval_errors(store: RunStore) -> None:
    run = await stored_run(store)
    approval = Approval(
        token=f"tok-{uuid.uuid4().hex}", run_id=run.id, step=0, tool="t", args={}, approver="bob"
    )
    await store.create_approval(approval)
    with pytest.raises(DuplicateRecord):
        await store.create_approval(approval)
    with pytest.raises(ApprovalNotFound):
        await store.get_approval("missing")
    with pytest.raises(ApprovalNotFound):
        await store.decide_approval("missing", "approved", T0, run=run)
    orphan = approval.model_copy(update={"token": "orphan", "run_id": "missing"})
    with pytest.raises(RunNotFound):
        await store.create_approval(orphan)


async def test_approval_is_spent_once_even_by_concurrent_spends(store: RunStore) -> None:
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    approval = Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={},
                        approver="bob", expires_at=T0 + timedelta(hours=1))  # fmt: skip
    await store.create_approval(approval)
    with pytest.raises(ApprovalNotSpendable):  # still pending
        await store.spend_approval(token, T0)
    await store.decide_approval(token, "approved", T0, run=run)
    spends = [store.spend_approval(token, T0 + timedelta(seconds=n)) for n in (1, 2)]
    results = await asyncio.gather(*spends, return_exceptions=True)
    spent = [r for r in results if isinstance(r, Approval)]
    assert len(spent) == 1
    assert [type(r) for r in results if not isinstance(r, Approval)] == [ApprovalNotSpendable]
    assert await store.get_approval(token) == spent[0]
    assert spent[0].expires_at == T0 + timedelta(hours=1)
    with pytest.raises(ApprovalNotFound):
        await store.spend_approval("missing", T0)


async def test_audit_events_append_and_list_in_order(store: RunStore) -> None:
    run = await stored_run(store)
    kinds = ["run.started", "tool.called", "gate.paused"]
    for i, kind in enumerate(kinds):
        event = AuditEvent(
            run_id=run.id,
            at=T0 + timedelta(seconds=i),
            principal_id=run.principal_id,
            kind=kind,  # type: ignore[arg-type]
            detail={"n": i},
        )
        await store.append_audit_event(event)

    listed = await store.list_audit_events(run.id)
    assert [e.kind for e in listed] == kinds
    assert [e.detail for e in listed] == [{"n": 0}, {"n": 1}, {"n": 2}]
    with pytest.raises(RunNotFound):
        await store.append_audit_event(listed[0].model_copy(update={"run_id": "missing"}))


async def test_audit_events_page_by_owner_run_and_acting_principal(store: RunStore) -> None:
    owner, bob, mallory = (unique_principal(n) for n in ("alice", "bob", "mallory"))
    mine, other = run_for(owner), run_for(bob)
    await store.create_run(mine)
    await store.create_run(other)
    for i, (run, who) in enumerate(
        [(mine, owner), (other, bob), (mine, mallory), (mine, owner)]
    ):  # fmt: skip
        await store.append_audit_event(
            AuditEvent(run_id=run.id, at=T0 + timedelta(seconds=i), principal_id=who,
                       kind="run.started", detail={"n": i})
        )  # fmt: skip

    def numbers(events: list[AuditEvent]) -> list[int]:
        return [e.detail["n"] for e in events]

    assert numbers(await store.page_audit_events(owner_id=owner)) == [0, 2, 3]
    assert numbers(await store.page_audit_events(owner_id=owner, limit=2)) == [0, 2]
    assert numbers(await store.page_audit_events(owner_id=owner, limit=2, offset=2)) == [3]
    by_actor = await store.page_audit_events(owner_id=owner, principal_id=mallory)
    assert numbers(by_actor) == [2]
    assert numbers(await store.page_audit_events(owner_id=owner, run_id=other.id)) == []
    assert numbers(await store.page_audit_events(owner_id=bob, run_id=other.id)) == [1]


@pytest.mark.parametrize("cls", [RunStore, InMemoryRunStore, PostgresRunStore])
def test_audit_events_have_no_update_or_delete_path(cls: type) -> None:
    """Appending and reading are the only ways to touch the log; paging is a read."""
    audit_methods = {n for n in dir(cls) if "audit" in n and not n.startswith("__")}
    assert audit_methods == {"append_audit_event", "list_audit_events", "page_audit_events"}


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET kind = 'run.failed'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
async def test_database_rejects_audit_rewrites(pg_engine: AsyncEngine, statement: str) -> None:
    pg_store = PostgresRunStore(pg_engine)
    run = await stored_run(pg_store)
    await pg_store.append_audit_event(
        AuditEvent(run_id=run.id, at=T0, principal_id=run.principal_id, kind="run.started")
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with pg_engine.begin() as conn:
            await conn.execute(text(statement))
    assert len(await pg_store.list_audit_events(run.id)) == 1
