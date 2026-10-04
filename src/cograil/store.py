"""RunStore: where Runs, tool calls, Approvals and AuditEvents are persisted.

AuditEvents are append-only. The protocol offers `append_audit_event` and
`list_audit_events` and nothing else; PostgresRunStore also has a database trigger
(see migrations/versions) that rejects UPDATE, DELETE and TRUNCATE on the table.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic_core import to_jsonable_python
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from cograil.domain import Approval, AuditEvent, Run, ToolCall
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    DuplicateRecord,
    RunNotFound,
)
from cograil.store_tables import approvals, audit_events, runs, tool_calls

ApprovalDecision = Literal["approved", "declined", "expired"]

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


@runtime_checkable
class RunStore(Protocol):
    async def create_run(self, run: Run) -> None: ...

    async def get_run(self, run_id: str) -> Run: ...

    async def update_run(self, run: Run) -> None:
        """Save status, cursor, context, cost_usd, harness_version and updated_at; identity
        fields never change. The runner stamps harness_version when it runs the Run."""
        ...

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None: ...

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]: ...

    async def create_approval(self, approval: Approval) -> None: ...

    async def get_approval(self, token: str) -> Approval: ...

    async def decide_approval(
        self, token: str, decision: ApprovalDecision, decided_at: datetime
    ) -> Approval:
        """Decide a pending Approval once; a second decision raises ApprovalAlreadyDecided."""
        ...

    async def spend_approval(self, token: str, spent_at: datetime) -> Approval:
        """Spend an approved Approval once, atomically: of two concurrent spends one wins and
        the other raises ApprovalNotSpendable, as does spending one not approved."""
        ...

    async def list_approvals(self, run_id: str) -> list[Approval]:
        """Ordered by token, the same in every implementation."""
        ...

    async def append_audit_event(self, event: AuditEvent) -> None: ...

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]: ...


_RUN_MUTABLE = ("status", "cursor", "context", "cost_usd", "harness_version", "updated_at")


class InMemoryRunStore:
    """Dict-backed RunStore for unit tests and local runs without a database."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._tool_calls: dict[str, list[ToolCall]] = {}
        self._approvals: dict[str, Approval] = {}
        self._audit: dict[str, list[AuditEvent]] = {}

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

    async def update_run(self, run: Run) -> None:
        self._require_run(run.id)
        changes = {name: getattr(run, name) for name in _RUN_MUTABLE}
        self._runs[run.id] = self._runs[run.id].model_copy(update=changes, deep=True)

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None:
        self._require_run(run_id)
        self._tool_calls.setdefault(run_id, []).append(call.model_copy(deep=True))

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        return [c.model_copy(deep=True) for c in self._tool_calls.get(run_id, [])]

    async def create_approval(self, approval: Approval) -> None:
        self._require_run(approval.run_id)
        if approval.token in self._approvals:
            raise DuplicateRecord(f"approval {approval.token}")
        self._approvals[approval.token] = approval.model_copy(deep=True)

    async def get_approval(self, token: str) -> Approval:
        if token not in self._approvals:
            raise ApprovalNotFound(token)
        return self._approvals[token].model_copy(deep=True)

    async def decide_approval(
        self, token: str, decision: ApprovalDecision, decided_at: datetime
    ) -> Approval:
        current = await self.get_approval(token)
        if current.decision != "pending":
            raise ApprovalAlreadyDecided(token)
        decided = current.model_copy(update={"decision": decision, "decided_at": decided_at})
        self._approvals[token] = decided
        return decided.model_copy(deep=True)

    async def spend_approval(self, token: str, spent_at: datetime) -> Approval:
        # No await between the check and the write, so concurrent spends cannot interleave.
        current = await self.get_approval(token)
        if current.decision != "approved" or current.spent_at is not None:
            raise ApprovalNotSpendable(token)
        spent = current.model_copy(update={"spent_at": spent_at})
        self._approvals[token] = spent
        return spent.model_copy(deep=True)

    async def list_approvals(self, run_id: str) -> list[Approval]:
        found = sorted((a for a in self._approvals.values() if a.run_id == run_id), key=_token)
        return [a.model_copy(deep=True) for a in found]

    async def append_audit_event(self, event: AuditEvent) -> None:
        self._require_run(event.run_id)
        self._audit.setdefault(event.run_id, []).append(event.model_copy(deep=True))

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]:
        return [e.model_copy(deep=True) for e in self._audit.get(run_id, [])]


def _token(approval: Approval) -> str:
    return approval.token


def _json(value: Any) -> Any:
    """Make a value safe for a JSONB column (datetimes, enums, nested models)."""
    return to_jsonable_python(value)


def _run_values(run: Run) -> dict[str, Any]:
    values = run.model_dump()
    for name in ("principal", "trigger", "context"):
        values[name] = _json(values[name])
    values["status"] = str(run.status)
    return values


def _call_values(run_id: str, call: ToolCall) -> dict[str, Any]:
    values = call.model_dump()
    values["args"] = _json(values["args"])
    values["result"] = _json(values["result"])
    return {"run_id": run_id, **values}


def _approval_values(approval: Approval) -> dict[str, Any]:
    values = approval.model_dump()
    values["args"] = _json(values["args"])
    return values


def _event_values(event: AuditEvent) -> dict[str, Any]:
    values = event.model_dump()
    values["detail"] = _json(values["detail"])
    return values


def _without(row: Mapping[Any, Any], *keys: str) -> dict[str, Any]:
    """A row as a dict, minus storage-only columns the domain model does not carry."""
    return {name: value for name, value in row.items() if name not in keys}


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


class PostgresRunStore:
    """RunStore on Postgres through SQLAlchemy async Core. Schema comes from Alembic."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str) -> PostgresRunStore:
        """`url` is a SQLAlchemy URL such as postgresql+asyncpg://user:pw@host/db."""
        return cls(create_async_engine(url))

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def _insert(
        self, table: Any, values: Mapping[str, Any], *, what: str, run_id: str
    ) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(insert(table).values(**values))
        except IntegrityError as exc:
            state = _sqlstate(exc)
            if state == _UNIQUE_VIOLATION:
                raise DuplicateRecord(what) from exc
            if state == _FOREIGN_KEY_VIOLATION:
                raise RunNotFound(run_id) from exc
            raise

    async def create_run(self, run: Run) -> None:
        await self._insert(runs, _run_values(run), what=f"run {run.id}", run_id=run.id)

    async def get_run(self, run_id: str) -> Run:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
        if row is None:
            raise RunNotFound(run_id)
        return Run.model_validate(dict(row))

    async def update_run(self, run: Run) -> None:
        values = _run_values(run)
        changes = {name: values[name] for name in _RUN_MUTABLE}
        async with self._engine.begin() as conn:
            result = await conn.execute(update(runs).where(runs.c.id == run.id).values(**changes))
        if result.rowcount == 0:
            raise RunNotFound(run.id)

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None:
        await self._insert(
            tool_calls, _call_values(run_id, call), what=f"tool call {call.tool}", run_id=run_id
        )

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        query = select(tool_calls).where(tool_calls.c.run_id == run_id).order_by(tool_calls.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [ToolCall.model_validate(_without(r, "id", "run_id")) for r in rows]

    async def create_approval(self, approval: Approval) -> None:
        await self._insert(
            approvals,
            _approval_values(approval),
            what=f"approval {approval.token}",
            run_id=approval.run_id,
        )

    async def get_approval(self, token: str) -> Approval:
        async with self._engine.connect() as conn:
            return await self._get_approval(conn, token)

    @staticmethod
    async def _get_approval(conn: AsyncConnection, token: str) -> Approval:
        query = select(approvals).where(approvals.c.token == token)
        row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise ApprovalNotFound(token)
        return Approval.model_validate(dict(row))

    async def decide_approval(
        self, token: str, decision: ApprovalDecision, decided_at: datetime
    ) -> Approval:
        pending = (approvals.c.token == token) & (approvals.c.decision == "pending")
        statement = (
            update(approvals)
            .where(pending)
            .values(decision=decision, decided_at=decided_at)
            .returning(approvals)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(statement)).mappings().first()
            if row is not None:
                return Approval.model_validate(dict(row))
            await self._get_approval(conn, token)  # raises ApprovalNotFound when absent
        raise ApprovalAlreadyDecided(token)

    async def spend_approval(self, token: str, spent_at: datetime) -> Approval:
        spendable = (
            (approvals.c.token == token)
            & (approvals.c.decision == "approved")
            & approvals.c.spent_at.is_(None)
        )
        statement = (
            update(approvals).where(spendable).values(spent_at=spent_at).returning(approvals)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(statement)).mappings().first()
            if row is not None:
                return Approval.model_validate(dict(row))
            await self._get_approval(conn, token)  # raises ApprovalNotFound when absent
        raise ApprovalNotSpendable(token)

    async def list_approvals(self, run_id: str) -> list[Approval]:
        query = select(approvals).where(approvals.c.run_id == run_id).order_by(approvals.c.token)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [Approval.model_validate(dict(r)) for r in rows]

    async def append_audit_event(self, event: AuditEvent) -> None:
        await self._insert(
            audit_events, _event_values(event), what="audit event", run_id=event.run_id
        )

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]:
        query = select(audit_events).where(audit_events.c.run_id == run_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query.order_by(audit_events.c.id))).mappings().all()
        return [AuditEvent.model_validate(_without(r, "id")) for r in rows]


__all__ = ["ApprovalDecision", "InMemoryRunStore", "PostgresRunStore", "RunStore"]
