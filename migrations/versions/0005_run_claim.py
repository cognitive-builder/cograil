"""Add runs.claim: the execution that may save a Run (issue #104).

`Runner.run` and `Runner.resume` each claim the Run with a fresh token by one conditional
UPDATE on the claim they read, so of two concurrent claims one wins. Every later save of the
Run is conditional on the claim too, so an execution whose claim was taken over stops at its
next save. Existing Runs start unclaimed (NULL), which the first claim replaces.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("claim", sa.Text))


def downgrade() -> None:
    op.drop_column("runs", "claim")
