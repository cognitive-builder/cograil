"""Alembic environment. Migrations connect as the table owner, read from
MIGRATIONS_DATABASE_URL, e.g. postgresql+asyncpg://cograil:cograil@localhost:5432/cograil.

DATABASE_URL is the fallback, for a database where the application also connects as the
owner. Where the application connects as cograil_app (migration 0006), DATABASE_URL names
that role, which may not create or alter tables, so MIGRATIONS_DATABASE_URL must be set.
"""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from cograil.store_tables import metadata

config = context.config
target_metadata = metadata


def _database_url() -> str:
    url = os.environ.get("MIGRATIONS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATIONS_DATABASE_URL and DATABASE_URL are not set")
    return url


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
