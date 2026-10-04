"""Shared fixtures: an in-memory RunStore holding one Run, and a CallContext inside it."""

from datetime import UTC, datetime

import pytest

from cograil.domain import Principal, Run, Trigger
from cograil.registry import CallContext
from cograil.store import InMemoryRunStore

T0 = datetime(2026, 10, 4, tzinfo=UTC)


@pytest.fixture
async def store() -> InMemoryRunStore:
    store = InMemoryRunStore()
    principal = Principal(id="alice@example.com", groups=["staff"])
    await store.create_run(
        Run(id="r1", workspace="example-smb", colleague="harper", protocol="leave_request",
            protocol_version=1, principal=principal, trigger=Trigger(kind="chat"),
            created_at=T0, updated_at=T0)
    )  # fmt: skip
    return store


@pytest.fixture
def ctx() -> CallContext:
    return CallContext(run_id="r1", step=2, principal_id="alice@example.com")
