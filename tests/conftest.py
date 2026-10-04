"""Shared fixtures: an in-memory RunStore holding one Run, a CallContext inside it, and a
migrated Postgres (DATABASE_URL) for the tests that need one."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from cograil.domain import Principal, Run, Trigger
from cograil.registry import CallContext
from cograil.store import InMemoryRunStore

T0 = datetime(2026, 10, 4, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


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
