"""RunStore: where Runs, tool calls, Approvals and AuditEvents are persisted.

AuditEvents are append-only. The protocol offers `append_audit_event` and
`list_audit_events` and nothing else; PostgresRunStore also has a database trigger
(see migrations/versions) that rejects UPDATE, DELETE and TRUNCATE on the table.

A Run is saved only by the execution holding its claim (`Run.claim`). `claim_run` takes the
claim over by compare-and-set on the claim and status the caller read, so of two executions
started from the same read exactly one goes on; every other save is conditional on the claim,
so an execution whose claim was taken over gets RunClaimLost at its next save and stops.
Creating and spending an Approval are conditional on the claim too (#143), so such an
execution neither spends an Approval nor leaves a pending one behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic_core import to_jsonable_python
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from cograil.domain import Approval, ApprovalDecision, AuditEvent, Run, RunStatus, ToolCall
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotSpendable,
    ApprovalRunMismatch,
    DuplicateRecord,
    RunClaimLost,
    RunNotFound,
)
from cograil.store_memory import RUN_MUTABLE, InMemoryRunStore
from cograil.store_tables import approvals, audit_events, runs, tool_calls

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


@runtime_checkable
class RunStore(Protocol):
    async def create_run(self, run: Run) -> None: ...

    async def get_run(self, run_id: str) -> Run: ...

    async def list_runs(self, limit: int = 20, *, principal_id: str | None = None) -> list[Run]:
        """The most recently created Runs first, at most `limit`; with `principal_id`, only
        the Runs that principal started."""
        ...

    async def update_run(self, run: Run) -> None:
        """Save status, cursor, context, cost_usd, harness_version and updated_at; identity
        fields never change. The runner stamps harness_version when it runs the Run.

        Saves only while the stored claim is still `run.claim`; otherwise RunClaimLost."""
        ...

    async def claim_run(self, run: Run, read: Run) -> None:
        """Save `run` as update_run does, and its new claim, if the stored claim and status are
        still those of `read`, the Run as the caller read it: of two claims made from one read
        one wins, and the other raises RunClaimLost, as does a claim from a stale read."""
        ...

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None: ...

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]: ...

    async def create_approval(self, approval: Approval, *, run: Run) -> None:
        """Create the pending `approval` of `run` and save `run` as update_run does,
        atomically: both or neither. RunClaimLost when the stored claim is no longer
        `run.claim`; ApprovalRunMismatch when `approval` belongs to another Run."""
        ...

    async def get_approval(self, token: str) -> Approval: ...

    async def decide_approval(
        self,
        token: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        *,
        run: Run,
        events: Sequence[AuditEvent] = (),
    ) -> Approval:
        """Decide a pending Approval of `run` once, save `run` as update_run does and append
        `events`, atomically: all of them or none. A second decision raises
        ApprovalAlreadyDecided; an Approval that belongs to another Run raises
        ApprovalRunMismatch."""
        ...

    async def spend_approval(self, token: str, spent_at: datetime, *, run: Run) -> Approval:
        """Spend an approved Approval of `run` once, atomically: of two concurrent spends one
        wins and the other raises ApprovalNotSpendable, as does spending one not approved.

        Spends only while the stored claim is still `run.claim`, which no claim can take over
        until the spend commits; otherwise RunClaimLost. ApprovalRunMismatch when the
        Approval belongs to another Run."""
        ...

    async def list_approvals(self, run_id: str) -> list[Approval]:
        """Ordered by token, the same in every implementation."""
        ...

    async def append_audit_event(self, event: AuditEvent) -> None: ...

    async def list_audit_events(self, run_id: str) -> list[AuditEvent]: ...

    async def page_audit_events(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEvent]:
        """AuditEvents of the Runs `owner_id` started, oldest first, `limit` from `offset`.

        `run_id` narrows to one Run and `principal_id` to the events that principal acted in.
        Read-only: the log stays append-only.
        """
        ...


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
        async with self._engine.begin() as conn:
            await self._insert_in(conn, table, values, what=what, run_id=run_id)

    @staticmethod
    async def _insert_in(
        conn: AsyncConnection, table: Any, values: Mapping[str, Any], *, what: str, run_id: str
    ) -> None:
        try:
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
            return await self._get_run(conn, run_id)

    @staticmethod
    async def _get_run(conn: AsyncConnection, run_id: str) -> Run:
        row = (await conn.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
        if row is None:
            raise RunNotFound(run_id)
        return Run.model_validate(dict(row))

    async def list_runs(self, limit: int = 20, *, principal_id: str | None = None) -> list[Run]:
        query = select(runs).order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(limit)
        if principal_id is not None:
            query = query.where(runs.c.principal_id == principal_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [Run.model_validate(dict(r)) for r in rows]

    async def update_run(self, run: Run) -> None:
        async with self._engine.begin() as conn:
            await self._update_run(conn, run, run.claim)

    async def claim_run(self, run: Run, read: Run) -> None:
        async with self._engine.begin() as conn:
            await self._update_run(conn, run, read.claim, read.status)

    @classmethod
    async def _update_run(
        cls, conn: AsyncConnection, run: Run, claim: str | None, status: RunStatus | None = None
    ) -> None:
        """One conditional UPDATE: a concurrent claim that committed first makes it match no
        row, since Postgres re-checks the WHERE clause against the committed row."""
        values = _run_values(run)
        changes = {name: values[name] for name in (*RUN_MUTABLE, "claim")}
        held = (runs.c.id == run.id) & runs.c.claim.is_not_distinct_from(claim)
        if status is not None:
            held &= runs.c.status == str(status)
        result = await conn.execute(update(runs).where(held).values(**changes))
        if result.rowcount == 0:
            await cls._get_run(conn, run.id)  # raises RunNotFound when absent
            raise RunClaimLost(run.id)

    async def record_tool_call(self, run_id: str, call: ToolCall) -> None:
        await self._insert(
            tool_calls, _call_values(run_id, call), what=f"tool call {call.tool}", run_id=run_id
        )

    async def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        query = select(tool_calls).where(tool_calls.c.run_id == run_id).order_by(tool_calls.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [ToolCall.model_validate(_without(r, "id", "run_id")) for r in rows]

    async def create_approval(self, approval: Approval, *, run: Run) -> None:
        if approval.run_id != run.id:
            raise ApprovalRunMismatch(approval.token)
        values, what = _approval_values(approval), f"approval {approval.token}"
        # The fenced save comes first and locks the Run's row, so a claim that took the Run
        # over makes it fail before the insert, and none can take it over before the commit.
        async with self._engine.begin() as conn:
            await self._update_run(conn, run, run.claim)
            await self._insert_in(conn, approvals, values, what=what, run_id=run.id)

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
        self,
        token: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        *,
        run: Run,
        events: Sequence[AuditEvent] = (),
    ) -> Approval:
        decidable = (
            (approvals.c.token == token)
            & (approvals.c.decision == "pending")
            & (approvals.c.run_id == run.id)
        )
        statement = (
            update(approvals)
            .where(decidable)
            .values(decision=decision, decided_at=decided_at)
            .returning(approvals)
        )
        # An error raised inside begin() rolls the decision back with the Run's save and the
        # AuditEvents, so a crash cannot leave a decided Approval without its AuditEvent.
        async with self._engine.begin() as conn:
            row = (await conn.execute(statement)).mappings().first()
            if row is None:
                current = await self._get_approval(conn, token)  # ApprovalNotFound when absent
                if current.run_id != run.id:
                    raise ApprovalRunMismatch(token)
                raise ApprovalAlreadyDecided(token)
            await self._update_run(conn, run, run.claim)
            await self._append(conn, events)
        return Approval.model_validate(dict(row))

    @staticmethod
    async def _append(conn: AsyncConnection, events: Sequence[AuditEvent]) -> None:
        for event in events:
            try:
                await conn.execute(insert(audit_events).values(**_event_values(event)))
            except IntegrityError as exc:
                if _sqlstate(exc) == _FOREIGN_KEY_VIOLATION:
                    raise RunNotFound(event.run_id) from exc
                raise

    async def spend_approval(self, token: str, spent_at: datetime, *, run: Run) -> Approval:
        spendable = (
            (approvals.c.token == token)
            & (approvals.c.run_id == run.id)
            & (approvals.c.decision == "approved")
            & approvals.c.spent_at.is_(None)
        )
        statement = (
            update(approvals).where(spendable).values(spent_at=spent_at).returning(approvals)
        )
        # Run row, then Approval row: decide_approval locks the other way round, but only
        # on a pending row, never the approved one a spend locks, so the two cannot deadlock.
        async with self._engine.begin() as conn:
            await self._hold_claim(conn, run)
            row = (await conn.execute(statement)).mappings().first()
            if row is not None:
                return Approval.model_validate(dict(row))
            current = await self._get_approval(conn, token)  # ApprovalNotFound when absent
        if current.run_id != run.id:
            raise ApprovalRunMismatch(token)
        raise ApprovalNotSpendable(token)

    @classmethod
    async def _hold_claim(cls, conn: AsyncConnection, run: Run) -> None:
        """Share-lock the Run's row if its stored claim is still `run.claim`: a claim taking
        the Run over waits for this transaction, and one that committed first, or was
        mid-commit, makes it match no row, so RunClaimLost."""
        held = (runs.c.id == run.id) & runs.c.claim.is_not_distinct_from(run.claim)
        query = select(runs.c.id).where(held).with_for_update(read=True)
        if (await conn.execute(query)).first() is None:
            await cls._get_run(conn, run.id)  # raises RunNotFound when absent
            raise RunClaimLost(run.id)

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

    async def page_audit_events(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEvent]:
        query = (
            select(audit_events)
            .join(runs, runs.c.id == audit_events.c.run_id)
            .where(runs.c.principal_id == owner_id)
        )
        if run_id is not None:
            query = query.where(audit_events.c.run_id == run_id)
        if principal_id is not None:
            query = query.where(audit_events.c.principal_id == principal_id)
        query = query.order_by(audit_events.c.id).limit(limit).offset(offset)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [AuditEvent.model_validate(_without(r, "id")) for r in rows]


__all__ = ["ApprovalDecision", "InMemoryRunStore", "PostgresRunStore", "RunStore"]
