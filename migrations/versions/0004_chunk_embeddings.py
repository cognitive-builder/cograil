"""Add chunks.embedding: the vector knowledge search ranks Chunks by (issue #23).

The column is nullable so existing Chunks survive the upgrade: the next
`cograil knowledge sync` rewrites every Chunk without an embedding, and search skips such
Chunks until then. There is deliberately no vector index: search ranks only the Chunks the
principal's groups allow (a materialized `acl_groups && :groups` subquery), and an
approximate index would rank first and filter after.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMENSIONS = 256  # store_tables.EMBEDDING_DIMENSIONS when this revision was written


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("chunks", sa.Column("embedding", VECTOR(DIMENSIONS)))


def downgrade() -> None:
    op.drop_column("chunks", "embedding")
