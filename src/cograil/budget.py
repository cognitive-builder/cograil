"""Monthly spending cap per workspace (ADR 0013, issue #56).

`harness.yaml`'s `budget.monthly_usd` caps what one workspace's Runs may cost in a calendar
month (UTC), so one client cannot spend another's budget. Spend is the sum of the recorded
`Run.cost_usd` of the workspace's Runs created in the month; nothing is estimated.

A Run that would start with the cap already spent is refused: the Runner escalates it with
reason `monthly_cap_reached` (`Gates.escalate`), which audits it with the Colleague's
escalation_contact. When a Run's execution ends with the month's spend at or over
`budget.alert_at` of the cap, one `budget.alerted` AuditEvent naming the contact is written;
the store counts the month's alerts, so later Runs find it and stay quiet. Delivering either
message is the channels' job, as for any escalation (gates.py).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from cograil.domain import AuditEvent, BudgetSettings, Colleague, Run
from cograil.gates import Clock
from cograil.observability import log_event
from cograil.store import RunStore


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """The calendar month (UTC) containing `now`, as [start, next month's start)."""
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


class Budget:
    """Checks a workspace's monthly cap for one Runner."""

    def __init__(
        self, store: RunStore, settings: BudgetSettings, colleague: Colleague, clock: Clock
    ) -> None:
        self._store = store
        self._settings = settings
        self._colleague = colleague
        self._clock = clock

    async def over_cap(self, run: Run) -> dict[str, Any] | None:
        """Detail for the escalation of a Run that may not start; None while there is budget."""
        cap = self._settings.monthly_usd
        if cap is None:
            return None
        spend = await self._spend(run)
        if spend >= cap:
            return {"workspace": run.workspace, "spend_usd": spend, "monthly_usd": cap}
        return None

    async def alert_if_crossed(self, run: Run) -> None:
        """Write the month's one `budget.alerted` AuditEvent once spend reaches alert_at."""
        cap = self._settings.monthly_usd
        if cap is None:
            return
        since, until = month_window(self._clock())
        usage = await self._store.month_usage(run.workspace, since, until)
        threshold = cap * self._settings.alert_at
        if usage.alerts or usage.spend_usd < threshold:
            return
        contact = self._colleague.escalation_contact
        detail = {"workspace": run.workspace, "month": f"{since:%Y-%m}", "contact": contact,
                  "spend_usd": usage.spend_usd, "monthly_usd": cap,
                  "alert_at": self._settings.alert_at}  # fmt: skip
        await self._store.append_audit_event(
            AuditEvent(run_id=run.id, at=self._clock(), principal_id=run.principal_id,
                       kind="budget.alerted", detail=detail)  # fmt: skip
        )
        log_event("budget.alerted", logging.WARNING, run_id=run.id, workspace=run.workspace,
                  contact=contact, spend_usd=usage.spend_usd)  # fmt: skip

    async def _spend(self, run: Run) -> float:
        since, until = month_window(self._clock())
        return (await self._store.month_usage(run.workspace, since, until)).spend_usd
