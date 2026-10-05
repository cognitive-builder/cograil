"""InMemoryRunStore: the dict-backed RunStore for unit tests and local runs without a database.

It follows the RunStore contract in cograil.store, which re-exports it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from cograil.domain import (
    BUDGET_ALERT_KIND,
    Approval,
    ApprovalDecision,
    AuditEvent,
    MonthUsage,
    Run,
    ToolCall,
)
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    ApprovalRunMismatch,
    DuplicateRecord,
    RunClaimLost,
    RunNotFound,
)

RUN_MUTABLE = (
    "status",
    "cursor",
    "context",
    "cost_usd",
    "harness_version",
    "tool_pack_version",
    "updated_at",
)


class InMemoryRunStore:
    """Dict-backed RunStore for unit tests and local runs without a database."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._tool_calls: dict[str, list[ToolCall]] = {}
        self._approvals: dict[str, Approval] = {}
        self._audit: list[AuditEvent] = []

    def _require_run(self, run_id: str) -> None:
        if run_id not in self._runs:
            raise RunNotFound(run_id)

    async def create_run(self, run: Run) -> None:
        if run.id in self._runs:
            raise DuplicateRecord(f"run {run.id}")
        self._runs[run.id] = run.model_copy(deep=True)

    async def get_run(self, run_id: str) -> Run:
        self._require_run(run_id)
        return self._runs[run_id].model_copy(deep=True)

    async def list_runs(self, limit: int = 20, *, principal_id: str | None = None) -> list[Run]:
        found = [r for r in self._runs.values() if principal_id in (None, r.principal_id)]
        newest = sorted(found, key=lambda r: (r.created_at, r.id), reverse=True)
        return [r.model_copy(deep=True) for r in newest[:limit]]

    async def month_usage(self, workspace: str, since: datetime, until: datetime) -> MonthUsage:
        mine = {r.id: r for r in self._runs.values() if r.workspace == workspace}
        spend = sum(r.cost_usd for r in mine.values() if since <= r.created_at < until)
        alerts = sum(
            1
            for e in self._audit
            if e.kind == BUDGET_ALERT_KIND and e.run_id in mine and since <= e.at < until
        )
        return MonthUsage(spend_usd=spend, alerts=alerts)

    async def update_run(self, run: Run) -> None:
        self._require_claim(run.id, run.claim)
        self._save_run(run)

    async def claim_run(self, run: Run, read: Run) -> None:
        # No await between the check and the write, so concurrent claims cannot interleave.
        self._require_claim(run.id, read.claim)
        if self._runs[run.id].status != read.status:
            raise RunClaimLost(run.id)
        self._save_run(run)

    def _require_claim(self, run_id: str, claim: str | None) -> None:
        self._require_run(run_id)
        if self._runs[run_id].claim != claim:
            raise RunClaimLost(run_id)

    def _save_run(self, run: Run) -> None:
        changes = {name: getattr(run, name) for name in (*RUN_MUTABLE, "claim")}
        self._runs[run.id] = self._runs[run.id].model_copy(update=changes, deep=True)

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None:
        self._require_run(run_id)
        self._tool_calls.setdefault(run_id, []).append(call.model_copy(deep=True))

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        return [c.model_copy(deep=True) for c in self._tool_calls.get(run_id, [])]

    async def create_approval(
        self, approval: Approval, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> None:
        # Every check comes before the first write, and no await between them.
        if approval.run_id != run.id:
            raise ApprovalRunMismatch(approval.token)
        self._require_claim(run.id, run.claim)
        if approval.token in self._approvals:
            raise DuplicateRecord(f"approval {approval.token}")
        self._require_runs_of(events)
        self._approvals[approval.token] = approval.model_copy(deep=True)
        self._save_run(run)
        self._append(events)

    async def get_approval(self, token: str) -> Approval:
        if token not in self._approvals:
            raise ApprovalNotFound(token)
        return self._approvals[token].model_copy(deep=True)

    def expire(self, token: str) -> None:
        """Test support: put an Approval past its deadline, as time running out would."""
        if token not in self._approvals:
            raise ApprovalNotFound(token)
        past = datetime.now(UTC) - timedelta(minutes=1)
        self._approvals[token] = self._approvals[token].model_copy(update={"expires_at": past})

    async def decide_approval(
        self,
        token: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        *,
        run: Run,
        events: Sequence[AuditEvent] = (),
    ) -> Approval:
        # Every check comes before the first write, and no await between them.
        current = await self.get_approval(token)
        if current.run_id != run.id:
            raise ApprovalRunMismatch(token)
        if current.decision != "pending":
            raise ApprovalAlreadyDecided(token)
        self._require_claim(run.id, run.claim)
        self._require_runs_of(events)
        decided = current.model_copy(update={"decision": decision, "decided_at": decided_at})
        self._approvals[token] = decided
        self._save_run(run)
        self._append(events)
        return decided.model_copy(deep=True)

    def _require_runs_of(self, events: Sequence[AuditEvent]) -> None:
        for event in events:
            self._require_run(event.run_id)

    def _append(self, events: Sequence[AuditEvent]) -> None:
        self._audit.extend(e.model_copy(deep=True) for e in events)

    async def spend_approval(
        self, token: str, spent_at: datetime, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> Approval:
        # No await between the checks and the write, so concurrent spends and claims cannot
        # interleave.
        self._require_claim(run.id, run.claim)
        current = await self.get_approval(token)
        if current.run_id != run.id:
            raise ApprovalRunMismatch(token)
        if current.decision != "approved" or current.spent_at is not None:
            raise ApprovalNotSpendable(token)
        self._require_runs_of(events)
        spent = current.model_copy(update={"spent_at": spent_at})
        self._approvals[token] = spent
        self._append(events)
        return spent.model_copy(deep=True)

    async def list_approvals(self, run_id: str) -> list[Approval]:
        found = sorted((a for a in self._approvals.values() if a.run_id == run_id), key=_token)
        return [a.model_copy(deep=True) for a in found]

    async def list_overdue_approvals(self, now: datetime) -> list[Approval]:
        found = sorted(
            (
                a
                for a in self._approvals.values()
                if a.decision == "pending" and a.expires_at is not None and a.expires_at <= now
            ),
            key=_token,
        )
        return [a.model_copy(deep=True) for a in found]

    async def append_audit_event(self, event: AuditEvent) -> None:
        self._require_run(event.run_id)
        self._audit.append(event.model_copy(deep=True))

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]:
        return [e.model_copy(deep=True) for e in self._audit if e.run_id == run_id]

    async def page_audit_events(
        self,
        *,
        owner_id: str | None,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEvent]:
        owned = {r.id for r in self._runs.values() if owner_id in (None, r.principal_id)}
        found = [
            e
            for e in self._audit
            if e.run_id in owned
            and run_id in (None, e.run_id)
            and principal_id in (None, e.principal_id)
        ]
        return [e.model_copy(deep=True) for e in found[offset : offset + limit]]


def _token(approval: Approval) -> str:
    return approval.token
