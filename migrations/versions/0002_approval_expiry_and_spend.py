"""Add approvals.expires_at and approvals.spent_at for gates (issue #11).

expires_at is when a pending Approval times out and its Run escalates. spent_at is set by
one conditional UPDATE when the Approval lets its one call through, so two concurrent
spends cannot both succeed.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("approvals", sa.Column("expires_at", _TZ))
    op.add_column("approvals", sa.Column("spent_at", _TZ))


def downgrade() -> None:
    op.drop_column("approvals", "spent_at")
    op.drop_column("approvals", "expires_at")
