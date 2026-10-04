"""Shared fixtures: an in-memory RunStore holding one Run, a CallContext inside it, and a
migrated Postgres (DATABASE_URL) for the tests that need one, reachable as its owner or as
the application role (cograil_app)."""

import asyncio
import os
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from cograil.domain import Principal, Run, Trigger
from cograil.registry import CallContext
from cograil.store import InMemoryRunStore

T0 = datetime(2026, 10, 4, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
APP_ROLE = "cograil_app"
APP_ROLE_PASSWORD = secrets.token_hex(16)  # one per test session, so modules agree on it


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


async def _set_app_role_password(owner_url: str) -> None:
    engine = create_async_engine(owner_url)
    async with engine.begin() as conn:  # ALTER ROLE takes no bind parameters; the hex is safe
        await conn.execute(text(f"ALTER ROLE {APP_ROLE} PASSWORD '{APP_ROLE_PASSWORD}'"))
    await engine.dispose()


@pytest.fixture(scope="module")
def app_role_url(migrated_url: str) -> str:
    """The migrated database as the application role, with a password set by its owner."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _set_app_role_password(migrated_url)).result()
    url = make_url(migrated_url).set(username=APP_ROLE, password=APP_ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)
