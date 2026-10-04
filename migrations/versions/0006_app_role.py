"""Create cograil_app: the role the application connects as (issue #74).

The append-only triggers on audit_events stop application code but not the table owner, who
can `ALTER TABLE audit_events DISABLE TRIGGER`. The application therefore connects as
cograil_app, which holds only INSERT and SELECT on audit_events and no more than the store
needs on the other tables; it owns nothing, so it can neither alter nor truncate a table.
Migrations keep connecting as the owner (MIGRATIONS_DATABASE_URL, see docs/deploy.md).

The role is created without a password, so nobody can log in as it until the owner sets one
with `ALTER ROLE cograil_app PASSWORD '...'`. Roles belong to the cluster, not the database,
so the upgrade leaves an existing cograil_app in place and the downgrade drops it only when
no other database still grants it anything. A later migration that adds a table grants
cograil_app what the application needs on it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "cograil_app"

# What each RunStore and KnowledgeStore statement needs, and nothing more. UPDATE on runs and
# approvals covers their SELECT ... FOR UPDATE; `cograil knowledge sync` rewrites chunks.
_TABLE_GRANTS = {
    "runs": "SELECT, INSERT, UPDATE",
    "tool_calls": "SELECT, INSERT",
    "approvals": "SELECT, INSERT, UPDATE",
    "audit_events": "SELECT, INSERT",
    "chunks": "SELECT, INSERT, UPDATE, DELETE",
}
_SEQUENCES = ("tool_calls_id_seq", "audit_events_id_seq")


def upgrade() -> None:
    op.execute(f"""DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN;
            END IF;
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {ROLE}', current_schema());
        END
        $$""")
    for table, privileges in _TABLE_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE {', '.join(_SEQUENCES)} TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON SEQUENCE {', '.join(_SEQUENCES)} FROM {ROLE}")
    op.execute(f"REVOKE ALL ON {', '.join(_TABLE_GRANTS)} FROM {ROLE}")
    op.execute(f"""DO $$
        BEGIN
            EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {ROLE}', current_schema());
            IF NOT EXISTS (
                SELECT FROM pg_shdepend JOIN pg_roles ON refobjid = pg_roles.oid
                WHERE rolname = '{ROLE}'
            ) THEN
                DROP ROLE {ROLE};
            END IF;
        END
        $$""")
