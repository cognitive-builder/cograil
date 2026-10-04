"""ProgressStore: a RunStore that reports each AuditEvent and finished Step as it is saved.

It wraps another RunStore and passes every call through unchanged, so the Runner, the gates
and the registry, which all write through the store, need no knowledge of it. `emit` receives
one line per AuditEvent and one per completed Step; the CLI prints them as the Run streams.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from cograil.domain import Approval, AuditEvent, Run, ToolCall
from cograil.store import ApprovalDecision, RunStore

_DETAIL_KEYS = ("step", "tool", "reason", "token", "approver", "decided_by", "error")


def describe(event: AuditEvent) -> str:
    """One line for an AuditEvent: its kind, who acted and the parts of its detail that matter."""
    parts = [f"{key}={event.detail[key]}" for key in _DETAIL_KEYS if event.detail.get(key)]
    return " ".join([event.kind, f"by={event.principal_id}", *parts])


class ProgressStore:
    def __init__(self, inner: RunStore, emit: Callable[[str], None]) -> None:
        self._inner = inner
        self._emit = emit
        self._cursors: dict[str, int] = {}

    def _emit_all(self, events: Sequence[AuditEvent]) -> None:
        for event in events:
            self._emit(describe(event))

    async def create_run(self, run: Run) -> None:
        self._cursors[run.id] = run.cursor
        await self._inner.create_run(run)

    async def get_run(self, run_id: str) -> Run:
        run = await self._inner.get_run(run_id)
        self._cursors.setdefault(run_id, run.cursor)
        return run

    async def list_runs(self, limit: int = 20, *, principal_id: str | None = None) -> list[Run]:
        return await self._inner.list_runs(limit, principal_id=principal_id)

    async def update_run(self, run: Run) -> None:
        await self._inner.update_run(run)
        if run.cursor > self._cursors.get(run.id, run.cursor):
            name = run.context.get("steps", {}).get(str(run.cursor), {}).get("name", "")
            self._emit(f"step {run.cursor} complete {name}".rstrip())
        self._cursors[run.id] = run.cursor

    async def claim_run(self, run: Run, read: Run) -> None:
        await self._inner.claim_run(run, read)

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None:
        await self._inner.record_tool_call(run_id, call)

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        return await self._inner.list_tool_calls(run_id)

    async def create_approval(
        self, approval: Approval, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> None:
        await self._inner.create_approval(approval, run=run, events=events)
        self._emit_all(events)

    async def get_approval(self, token: str) -> Approval:
        return await self._inner.get_approval(token)

    async def decide_approval(
        self,
        token: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        *,
        run: Run,
        events: Sequence[AuditEvent] = (),
    ) -> Approval:
        decided = await self._inner.decide_approval(
            token, decision, decided_at, run=run, events=events
        )
        self._emit_all(events)
        return decided

    async def spend_approval(
        self, token: str, spent_at: datetime, *, run: Run, events: Sequence[AuditEvent] = ()
    ) -> Approval:
        spent = await self._inner.spend_approval(token, spent_at, run=run, events=events)
        self._emit_all(events)
        return spent

    async def list_approvals(self, run_id: str) -> list[Approval]:
        return await self._inner.list_approvals(run_id)

    async def append_audit_event(self, event: AuditEvent) -> None:
        await self._inner.append_audit_event(event)
        self._emit(describe(event))

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]:
        return await self._inner.list_audit_events(run_id)

    async def page_audit_events(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEvent]:
        return await self._inner.page_audit_events(
            owner_id=owner_id, run_id=run_id, principal_id=principal_id, limit=limit, offset=offset
        )
