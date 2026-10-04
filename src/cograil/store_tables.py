"""SQLAlchemy Core table definitions for PostgresRunStore.

The Alembic migration in migrations/versions is the source of truth for the schema;
these definitions only describe it to the query builder.
"""

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData()

EMBEDDING_DIMENSIONS = 256
"""The width of `chunks.embedding`; an Embedder must give vectors this wide (migration 0004)."""

runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("workspace", Text, nullable=False),
    Column("colleague", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("protocol_version", Integer, nullable=False),
    Column("harness_version", Text, nullable=False),
    Column("principal", JSONB, nullable=False),
    Column("principal_id", Text, nullable=False),
    Column("trigger", JSONB, nullable=False),
    Column("trigger_kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("cursor", Integer, nullable=False),
    Column("context", JSONB, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("claim", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Text, ForeignKey("runs.id"), nullable=False),
    Column("step", Integer, nullable=False),
    Column("tool", Text, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("result", JSONB),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Index("ix_tool_calls_run_id", "run_id", "id"),
)

approvals = Table(
    "approvals",
    metadata,
    Column("token", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("runs.id"), nullable=False),
    Column("step", Integer, nullable=False),
    Column("tool", Text, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("approver", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("spent_at", DateTime(timezone=True)),
    Index("ix_approvals_run_id", "run_id"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Text, ForeignKey("runs.id"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("principal_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB, nullable=False),
    Index("ix_audit_events_run_id", "run_id", "id"),
)

chunks = Table(
    "chunks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("acl_groups", ARRAY(Text), nullable=False),
    Column("embedding", VECTOR(EMBEDDING_DIMENSIONS)),
    Index("ix_chunks_source", "source"),
    Index("ix_chunks_acl_groups", "acl_groups", postgresql_using="gin"),
)
