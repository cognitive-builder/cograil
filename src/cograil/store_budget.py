"""The Postgres query behind `RunStore.month_usage` (kept out of store.py to keep it small)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cograil.domain import BUDGET_ALERT_KIND, MonthUsage
from cograil.store_tables import audit_events, runs


async def month_usage(
    conn: AsyncConnection, workspace: str, since: datetime, until: datetime
) -> MonthUsage:
    """Dollars of the workspace's Runs created in [since, until), and the alerts sent in it."""
    spend = select(func.coalesce(func.sum(runs.c.cost_usd), 0.0)).where(
        runs.c.workspace == workspace, runs.c.created_at >= since, runs.c.created_at < until
    )
    alerts = (
        select(func.count())
        .select_from(audit_events.join(runs, audit_events.c.run_id == runs.c.id))
        .where(
            runs.c.workspace == workspace,
            audit_events.c.kind == BUDGET_ALERT_KIND,
            audit_events.c.at >= since,
            audit_events.c.at < until,
        )
    )
    return MonthUsage(
        spend_usd=float((await conn.execute(spend)).scalar_one()),
        alerts=int((await conn.execute(alerts)).scalar_one()),
    )
