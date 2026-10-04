"""Claims: how an execution takes a Run over, saves it, and fails it closed.

`Runner.run` and `Runner.resume` claim the Run (`RunStore.claim_run`) with a fresh claim
token: of two executions only the last claim's saves land, and the other stops with
RunClaimLost without failing the Run. Any other error fails the Run closed, if this
execution still holds its claim: status failed, a `run.failed` AuditEvent with the
principal, the cursor left at the last completed Step, and the error re-raised.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from cograil import observability
from cograil.domain import AuditEvent, Run, RunStatus
from cograil.errors import RunClaimLost
from cograil.gates import Clock
from cograil.observability import log_event
from cograil.redaction import Redactor
from cograil.store import RunStore

RunAuditKind = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "tier.escalated",
    "context.compressed",
    "context.screened",
]


class RunClaims:
    """Claims, saves and audits a Run for one Runner, stamping each change with the clock."""

    def __init__(self, store: RunStore, clock: Clock, redactor: Redactor | None = None) -> None:
        self._store = store
        self._clock = clock
        self._redactor = redactor or Redactor()

    @asynccontextmanager
    async def failing_closed(self, run_id: str) -> AsyncIterator[str]:
        """Yield a fresh claim token; fail the Run closed on any error but RunClaimLost."""
        token, claim = observability.run_id.set(run_id), secrets.token_hex(16)
        try:
            yield claim
        except RunClaimLost:
            raise  # another execution holds the Run; failing it is not this one's to do
        except Exception as exc:
            await self._fail(run_id, claim, exc)
            raise
        finally:
            observability.run_id.reset(token)

    async def claim(self, run: Run, claim: str, **changes: Any) -> Run:
        """Take the Run over for this execution, from the Run as it was read."""
        claimed = run.model_copy(update={**changes, "status": RunStatus.running, "claim": claim,
                                         "updated_at": self._clock()})  # fmt: skip
        await self._store.claim_run(claimed, run)
        return claimed

    async def save(self, run: Run, **changes: Any) -> Run:
        """Save the Run with these changes; RunClaimLost if another execution holds it."""
        run = run.model_copy(update={**changes, "updated_at": self._clock()})
        await self._store.update_run(run)
        return run

    async def audit(self, run: Run, kind: RunAuditKind, detail: dict[str, Any]) -> None:
        event = AuditEvent(
            run_id=run.id, at=self._clock(), principal_id=run.principal_id, kind=kind, detail=detail
        )
        await self._store.append_audit_event(event)

    async def _fail(self, run_id: str, claim: str, exc: Exception) -> None:
        """Fail the Run closed if this execution holds its claim; otherwise leave it as is.

        A Run taken over between the read and the save is the winner's to fail, so the
        failure is only logged and `exc`, the real failure, is what the caller still sees.
        """
        run = await self._store.get_run(run_id)
        if run.claim != claim:
            return
        try:
            run = await self.save(run, status=RunStatus.failed)
        except RunClaimLost:
            log_event("run.fail.skipped", logging.WARNING, error=type(exc).__name__,
                      reason="claim_lost")  # fmt: skip
            return
        message = await self._redactor.redact(str(exc))  # may carry a tool's error text
        detail = {"error": type(exc).__name__, "message": message, "cursor": run.cursor}
        await self.audit(run, "run.failed", detail)
        log_event("run.failed", logging.WARNING, error=type(exc).__name__, cursor=run.cursor)
