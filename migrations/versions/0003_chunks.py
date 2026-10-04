"""Create chunks: the slices of a KnowledgeSource that `cograil knowledge sync` stores (#22).

acl_groups is a text array with a GIN index so retrieval can pre-filter on the principal's
groups (`acl_groups && :groups`) before it ranks anything (issue #23).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_uri", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("acl_groups", ARRAY(sa.Text), nullable=False),
    )
    op.create_index("ix_chunks_source", "chunks", ["source"])
    op.create_index("ix_chunks_acl_groups", "chunks", ["acl_groups"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunks_acl_groups", table_name="chunks")
    op.drop_index("ix_chunks_source", table_name="chunks")
    op.drop_table("chunks")
