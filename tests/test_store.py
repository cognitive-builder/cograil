"""RunStore contract, run against InMemoryRunStore and, when DATABASE_URL is set, Postgres."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import Approval, AuditEvent, Principal, Run, RunStatus, ToolCall, Trigger
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    ApprovalRunMismatch,
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
    params=[
        "memory",
        pytest.param("postgres", marks=pytest.mark.integration),
        pytest.param("postgres-app", marks=pytest.mark.integration),  # as cograil_app
    ],
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[RunStore]:
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    # An async fixture cannot request another async fixture by name, so this one owns its engine.
    url = "app_role_url" if request.param == "postgres-app" else "migrated_url"
    pg_store = PostgresRunStore.from_url(request.getfixturevalue(url))
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
            "tool_pack_version": "9e1c" * 16,
            "updated_at": T0 + timedelta(minutes=1),
        }
    )
    await store.update_run(progressed)
    assert await store.get_run(run.id) == progressed


async def test_list_runs_newest_first_up_to_the_limit(store: RunStore) -> None:
    ids, alice = [], unique_principal("alice")  # Postgres keeps the other params' Runs
    for minutes in (0, 2, 1):
        run = run_for(alice).model_copy(update={"created_at": T0 + timedelta(minutes=minutes)})
        await store.create_run(run)
        ids.append(run.id)
    listed = await store.list_runs(limit=2, principal_id=alice)
    assert [r.id for r in listed] == [ids[1], ids[2]]


async def test_list_runs_can_be_limited_to_one_principal(store: RunStore) -> None:
    alice, bob = unique_principal("alice"), unique_principal("bob")
    mine, other = run_for(alice), run_for(bob)
    await store.create_run(mine)
    await store.create_run(other)
    assert [r.id for r in await store.list_runs(principal_id=alice)] == [mine.id]
    assert [r.id for r in await store.list_runs(principal_id=bob)] == [other.id]
    assert await store.list_runs(principal_id=unique_principal("nobody")) == []


async def test_month_usage_sums_one_workspaces_runs_and_alerts_in_the_span(
    store: RunStore,
) -> None:
    workspace = f"ws-{uuid.uuid4().hex}"  # Postgres rows outlive a test inside one CI job
    month = (T0, T0 + timedelta(days=30))
    ids = {}
    for name, ws, cost, at in [
        ("now", workspace, 0.25, T0),
        ("later", workspace, 0.5, T0 + timedelta(days=29)),
        ("before", workspace, 9.0, T0 - timedelta(seconds=1)),
        ("after", workspace, 9.0, T0 + timedelta(days=30)),
        ("other", f"other-{workspace}", 9.0, T0),
    ]:
        run = make_run().model_copy(update={"workspace": ws, "cost_usd": cost, "created_at": at})
        await store.create_run(run)
        ids[name] = run.id
    alert = AuditEvent(run_id=ids["now"], at=T0, principal_id="alice@example.com",
                       kind="budget.alerted", detail={})  # fmt: skip
    await store.append_audit_event(alert)
    await store.append_audit_event(alert.model_copy(update={"run_id": ids["other"]}))
    usage = await store.month_usage(workspace, *month)
    assert (usage.spend_usd, usage.alerts) == (pytest.approx(0.75), 1)


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
    await store.create_approval(pending, run=run)
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
    """Issue #95: the decision and the Run's save happen together, or neither does.

    The failing half now shows the #105 pairing guard: another Run decides nothing."""

    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    await store.create_approval(
        Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={}, approver="bob"),
        run=run,
    )
    paused = run.model_copy(update={"status": RunStatus.awaiting_approval})
    await store.update_run(paused)
    resumed = paused.model_copy(update={"status": RunStatus.running, "updated_at": T0})

    lost = resumed.model_copy(update={"id": "missing"})  # not this Approval's Run (#105)
    with pytest.raises(ApprovalRunMismatch):
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
        Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={}, approver="bob"),
        run=run,
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


async def test_an_approval_is_decided_only_with_its_own_run(store: RunStore) -> None:
    """Issue #105: an Approval decided with another Run's snapshot changes nothing."""
    mine, other = make_run(), make_run()
    await store.create_run(mine)
    await store.create_run(other)
    token = f"tok-{uuid.uuid4().hex}"
    await store.create_approval(
        Approval(token=token, run_id=mine.id, step=1, tool="hris.book", args={}, approver="bob"),
        run=mine,
    )
    resumed = other.model_copy(update={"status": RunStatus.running, "updated_at": T0})
    event = AuditEvent(
        run_id=other.id, at=T0, principal_id="bob", kind="gate.resumed", detail={"token": token}
    )
    with pytest.raises(ApprovalRunMismatch):
        await store.decide_approval(token, "approved", T0, run=resumed, events=[event])
    assert (await store.get_approval(token)).decision == "pending"
    assert await store.get_run(mine.id) == mine
    assert await store.get_run(other.id) == other
    assert await store.list_audit_events(other.id) == []

    decided = await store.decide_approval(token, "approved", T0, run=mine)
    assert decided.decision == "approved"
    assert await store.get_run(other.id) == other


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
    await store.create_approval(approval, run=run)
    with pytest.raises(DuplicateRecord):  # and the Run's save is rolled back with it
        await store.create_approval(approval, run=run.model_copy(update={"cursor": 1}))
    assert await store.get_run(run.id) == run
    with pytest.raises(ApprovalNotFound):
        await store.get_approval("missing")
    with pytest.raises(ApprovalNotFound):
        await store.decide_approval("missing", "approved", T0, run=run)
    orphan = approval.model_copy(update={"token": "orphan", "run_id": "missing"})
    with pytest.raises(RunNotFound):
        await store.create_approval(orphan, run=make_run("missing"))


async def test_approval_is_spent_once_even_by_concurrent_spends(store: RunStore) -> None:
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    approval = Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={},
                        approver="bob", expires_at=T0 + timedelta(hours=1))  # fmt: skip
    await store.create_approval(approval, run=run)
    with pytest.raises(ApprovalNotSpendable):  # still pending
        await store.spend_approval(token, T0, run=run)
    await store.decide_approval(token, "approved", T0, run=run)
    spends = [store.spend_approval(token, T0 + timedelta(seconds=n), run=run) for n in (1, 2)]
    results = await asyncio.gather(*spends, return_exceptions=True)
    spent = [r for r in results if isinstance(r, Approval)]
    assert len(spent) == 1
    assert [type(r) for r in results if not isinstance(r, Approval)] == [ApprovalNotSpendable]
    assert await store.get_approval(token) == spent[0]
    assert spent[0].expires_at == T0 + timedelta(hours=1)
    with pytest.raises(ApprovalNotFound):
        await store.spend_approval("missing", T0, run=run)


async def test_overdue_approvals_are_the_pending_ones_past_their_expiry(store: RunStore) -> None:
    """Issue #282: the sweep finds them with one read, across Runs, and writes nothing."""
    run = await stored_run(store)
    tag = uuid.uuid4().hex

    def approval(name: str, expires_at: datetime | None) -> Approval:
        return Approval(token=f"{name}-{tag}", run_id=run.id, step=1, tool="hris.book", args={},
                        approver="bob", expires_at=expires_at)  # fmt: skip

    now = T0 + timedelta(hours=2)
    made = {
        "due": approval("due", now - timedelta(minutes=1)),
        "exact": approval("exact", now),
        "later": approval("later", now + timedelta(minutes=1)),
        "never": approval("never", None),
        "decided": approval("decided", now - timedelta(hours=1)),
    }
    for one in made.values():
        await store.create_approval(one, run=run)
    await store.decide_approval(made["decided"].token, "declined", T0, run=run)

    mine = {a.token for a in await store.list_overdue_approvals(now) if a.token.endswith(tag)}
    assert mine == {made["due"].token, made["exact"].token}
    assert (await store.get_approval(made["due"].token)).decision == "pending"


async def test_an_approval_is_created_only_by_the_execution_holding_the_claim(
    store: RunStore,
) -> None:
    """Issue #143: the pending Approval and the paused Run are saved together, and only while
    the stored claim is still the caller's, so a taken-over execution leaves no Approval."""
    run = await stored_run(store)
    approval = Approval(token=f"tok-{uuid.uuid4().hex}", run_id=run.id, step=1,
                        tool="hris.book", args={}, approver="bob")  # fmt: skip
    paused = run.model_copy(update={"status": RunStatus.awaiting_approval, "updated_at": T0})
    with pytest.raises(RunClaimLost):
        await store.create_approval(approval, run=paused.model_copy(update={"claim": "lost"}))
    with pytest.raises(ApprovalRunMismatch):
        await store.create_approval(approval, run=paused.model_copy(update={"id": "other"}))
    assert await store.list_approvals(run.id) == []
    assert await store.get_run(run.id) == run

    await store.create_approval(approval, run=paused)
    assert await store.list_approvals(run.id) == [approval]
    assert await store.get_run(run.id) == paused


async def test_an_approval_is_spent_only_by_the_execution_holding_the_claim(
    store: RunStore,
) -> None:
    """Issue #143: an execution whose claim was taken over cannot spend the Run's Approval;
    the execution that took it over still can, once."""
    run, other = await stored_run(store), await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    approval = Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={},
                        approver="bob", decision="approved")  # fmt: skip
    await store.create_approval(approval, run=run)
    taken = run.model_copy(update={"status": RunStatus.running, "claim": "winner"})
    await store.claim_run(taken, run)

    with pytest.raises(RunClaimLost):
        await store.spend_approval(token, T0, run=run)
    with pytest.raises(ApprovalRunMismatch):
        await store.spend_approval(token, T0, run=other)
    assert (await store.get_approval(token)).spent_at is None

    spent = await store.spend_approval(token, T0, run=taken)
    assert spent.spent_at == T0
    with pytest.raises(ApprovalNotSpendable):
        await store.spend_approval(token, T0, run=taken)


async def test_creating_and_spending_an_approval_write_their_audit_events_with_them(
    store: RunStore,
) -> None:
    """Issue #173: the pause and the spend are saved with their AuditEvents or not at all."""
    run = await stored_run(store)
    token = f"tok-{uuid.uuid4().hex}"
    approval = Approval(token=token, run_id=run.id, step=1, tool="hris.book", args={},
                        approver="bob")  # fmt: skip
    paused = run.model_copy(update={"status": RunStatus.awaiting_approval, "updated_at": T0})

    def event(kind: Literal["gate.paused", "gate.spent"]) -> AuditEvent:
        return AuditEvent(run_id=run.id, at=T0, principal_id=run.principal_id, kind=kind,
                          detail={"token": token})  # fmt: skip

    pause, spend = event("gate.paused"), event("gate.spent")
    unwritable = pause.model_copy(update={"run_id": "missing"})
    with pytest.raises(RunNotFound):  # rolls back the pending Approval and the paused Run
        await store.create_approval(approval, run=paused, events=[pause, unwritable])
    assert await store.list_approvals(run.id) == []
    assert await store.get_run(run.id) == run
    assert await store.list_audit_events(run.id) == []

    await store.create_approval(approval, run=paused, events=[pause])
    assert await store.get_run(run.id) == paused
    await store.decide_approval(token, "approved", T0, run=paused)
    with pytest.raises(RunNotFound):  # rolls back the spend
        await store.spend_approval(token, T0, run=paused, events=[spend, unwritable])
    assert (await store.get_approval(token)).spent_at is None
    assert await store.list_audit_events(run.id) == [pause]

    await store.spend_approval(token, T0, run=paused, events=[spend])
    assert (await store.get_approval(token)).spent_at == T0
    assert await store.list_audit_events(run.id) == [pause, spend]


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
    # no owner (an auditor's read): every Run's events; the shared database may hold more
    everyone = await store.page_audit_events(owner_id=None, limit=200)
    assert numbers([e for e in everyone if e.run_id in (mine.id, other.id)]) == [0, 1, 2, 3]
    assert numbers(await store.page_audit_events(owner_id=None, run_id=other.id)) == [1]


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
