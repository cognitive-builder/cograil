"""The application role holds only INSERT and SELECT on audit_events (issue #74).

The append-only triggers stop UPDATE, DELETE and TRUNCATE from the owner too, but the owner
could disable them. cograil_app owns nothing, so Postgres refuses each of those, and the
ALTER TABLE that would disable a trigger, before any trigger runs.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cograil.domain import AuditEvent, Principal, Run, Trigger
from cograil.store import PostgresRunStore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
async def app_engine(app_role_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_role_url)
    yield engine
    await engine.dispose()


async def audited_run(store: PostgresRunStore) -> Run:
    run = Run(
        id=f"run-{uuid.uuid4().hex}",
        workspace="example-smb",
        colleague="hr",
        protocol="leave_request",
        protocol_version=1,
        principal=Principal(id="alice@example.com", groups=["staff"]),
        trigger=Trigger(kind="chat"),
        created_at=T0,
        updated_at=T0,
    )
    await store.create_run(run)
    await store.append_audit_event(
        AuditEvent(run_id=run.id, at=T0, principal_id=run.principal_id, kind="run.started")
    )
    return run


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET kind = 'run.failed'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete",
        "ALTER TABLE audit_events DISABLE TRIGGER ALL",
    ],
)
async def test_the_app_role_cannot_rewrite_the_audit_trail(
    app_engine: AsyncEngine, statement: str
) -> None:
    store = PostgresRunStore(app_engine)
    run = await audited_run(store)
    with pytest.raises(DBAPIError) as refused:
        async with app_engine.begin() as conn:
            await conn.execute(text(statement))
    assert getattr(refused.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
    assert [e.kind for e in await store.list_audit_events(run.id)] == ["run.started"]
