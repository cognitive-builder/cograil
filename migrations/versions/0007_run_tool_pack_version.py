"""Add runs.tool_pack_version: the tool pack a Run started with (issue #110).

`Runner.run` stamps the hash of the tool pack (the validated Tools of tools.yaml and the
workspace python modules they resolve to) next to the harness version, and `Runner.resume`
refuses a Run whose tool pack has changed since. Existing Runs get "unversioned", which no
built tool pack has, so a Run paused before this migration cannot be resumed; its Approval
can only expire.

cograil_app's table-wide SELECT, INSERT and UPDATE on runs (migration 0006) cover the column.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("tool_pack_version", sa.Text, nullable=False, server_default="unversioned"),
    )


def downgrade() -> None:
    op.drop_column("runs", "tool_pack_version")
