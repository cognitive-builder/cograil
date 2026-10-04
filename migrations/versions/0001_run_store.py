"""Create the RunStore tables: runs, tool_calls, approvals, audit_events.

audit_events is append-only: triggers reject UPDATE, DELETE and TRUNCATE, so no code path,
including a future bug, can rewrite the audit trail.

Revision ID: 0001
Revises:
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def _create_runs() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("colleague", sa.Text, nullable=False),
        sa.Column("protocol", sa.Text, nullable=False),
        sa.Column("protocol_version", sa.Integer, nullable=False),
        sa.Column("harness_version", sa.Text, nullable=False),
        sa.Column("principal", JSONB, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("trigger", JSONB, nullable=False),
        sa.Column("trigger_kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("cursor", sa.Integer, nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )


def _create_run_children() -> None:
    run_fk = sa.ForeignKey("runs.id")
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, run_fk, nullable=False),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args", JSONB, nullable=False),
        sa.Column("result", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("started_at", _TZ, nullable=False),
        sa.Column("ended_at", _TZ),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id", "id"])
    op.create_table(
        "approvals",
        sa.Column("token", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args", JSONB, nullable=False),
        sa.Column("approver", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("decided_at", _TZ),
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])


def _create_audit_events() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("at", _TZ, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("detail", JSONB, nullable=False),
    )
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id", "id"])
    op.execute(
        """
        CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_truncate
        BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()
        """
    )


def upgrade() -> None:
    _create_runs()
    _create_run_children()
    _create_audit_events()


def downgrade() -> None:
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.drop_table("approvals")
    op.drop_table("tool_calls")
    op.drop_table("runs")
