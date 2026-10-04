"""RunStore contract, run against InMemoryRunStore and, when DATABASE_URL is set, Postgres."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import Approval, AuditEvent, Principal, Run, RunStatus, ToolCall, Trigger
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    DuplicateRecord,
    RunNotFound,
)
from cograil.store import InMemoryRunStore, PostgresRunStore, RunStore

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # env.py calls asyncio.run, which needs a thread free of a running event loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(command.upgrade, config, "head").result()
    yield url


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


async def test_run_round_trips_and_update_saves_progress(store: RunStore) -> None:
    run = await stored_run(store)
    assert await store.get_run(run.id) == run

    progressed = run.model_copy(
        update={
            "status": RunStatus.awaiting_approval,
            "cursor": 2,
            "context": {"days": 3, "approved": False},
            "cost_usd": 0.0125,
            "updated_at": T0 + timedelta(minutes=1),
        }
    )
    await store.update_run(progressed)
    assert await store.get_run(run.id) == progressed


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

    decided = await store.decide_approval(token, "approved", T0)
    assert decided.decision == "approved"
    assert decided.decided_at == T0
    assert await store.get_approval(token) == decided

    with pytest.raises(ApprovalAlreadyDecided):
        await store.decide_approval(token, "declined", T0)
    assert (await store.get_approval(token)).decision == "approved"


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
        await store.decide_approval("missing", "approved", T0)
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
    await store.decide_approval(token, "approved", T0)
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


@pytest.mark.parametrize("cls", [RunStore, InMemoryRunStore, PostgresRunStore])
def test_audit_events_have_no_update_or_delete_path(cls: type) -> None:
    audit_methods = {n for n in dir(cls) if "audit" in n and not n.startswith("__")}
    assert audit_methods == {"append_audit_event", "list_audit_events"}


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
