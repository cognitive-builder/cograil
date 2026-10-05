"""Read queries of PostgresRunStore, kept out of store.py to keep it small."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cograil.domain import Approval, AuditEvent
from cograil.store_rows import without
from cograil.store_tables import approvals, audit_events, runs


async def overdue_approvals(conn: AsyncConnection, workspace: str, now: datetime) -> list[Approval]:
    """Pending Approvals of the workspace's Runs whose `expires_at` is at or before `now`."""
    query = (
        select(approvals)
        .join(runs, runs.c.id == approvals.c.run_id)
        .where(
            runs.c.workspace == workspace,
            approvals.c.decision == "pending",
            approvals.c.expires_at.is_not(None),
            approvals.c.expires_at <= now,
        )
        .order_by(approvals.c.token)
    )
    rows = (await conn.execute(query)).mappings().all()
    return [Approval.model_validate(dict(r)) for r in rows]


async def audit_events_page(
    conn: AsyncConnection,
    owner_id: str | None,
    run_id: str | None,
    principal_id: str | None,
    limit: int,
    offset: int,
) -> list[AuditEvent]:
    """AuditEvents oldest first, narrowed as `RunStore.page_audit_events` says."""
    query = select(audit_events).join(runs, runs.c.id == audit_events.c.run_id)
    if owner_id is not None:
        query = query.where(runs.c.principal_id == owner_id)
    if run_id is not None:
        query = query.where(audit_events.c.run_id == run_id)
    if principal_id is not None:
        query = query.where(audit_events.c.principal_id == principal_id)
    query = query.order_by(audit_events.c.id).limit(limit).offset(offset)
    rows = (await conn.execute(query)).mappings().all()
    return [AuditEvent.model_validate(without(r, "id")) for r in rows]
