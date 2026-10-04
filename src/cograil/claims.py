"""Claims: how an execution takes a Run over, saves it, and fails it closed.

`Runner.run` and `Runner.resume` claim the Run (`RunStore.claim_run`) with a fresh claim
token: of two executions only the last claim's saves land, and the other stops with
RunClaimLost without failing the Run. Any other error fails the Run closed, if this
execution still holds its claim: status failed, a `run.failed` AuditEvent with the
principal, the cursor left at the last completed Step, and the error re-raised.

An execution's spend is kept here, by its claim, as the model calls are charged (issue #221):
`charge` for a call made with the Run in hand, the `Charge` of `charge_to` for one made
without it (a redaction). `settled` shows it on a Run, and `save` saves it, so a Run that
escalates or fails mid-Step, its in-memory Run lost with the error, still shows every call.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from cograil import observability
from cograil.cost import Charge, Spend, add_call, spend_of, with_spend
from cograil.domain import AuditEvent, Harness, Run, RunStatus
from cograil.errors import RunClaimLost
from cograil.gates import Clock
from cograil.observability import log_event
from cograil.providers.base import Usage
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

    def __init__(
        self, store: RunStore, clock: Clock, harness: Harness, redactor: Redactor | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        self._harness = harness
        self._redactor = redactor or Redactor()
        self._spent: dict[str, Spend] = {}  # by claim: what each execution's Run has spent

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
            self._spent.pop(claim, None)
            observability.run_id.reset(token)

    async def claim(self, run: Run, claim: str, **changes: Any) -> Run:
        """Take the Run over for this execution, from the Run as it was read."""
        claimed = run.model_copy(update={**changes, "status": RunStatus.running, "claim": claim,
                                         "updated_at": self._clock()})  # fmt: skip
        await self._store.claim_run(claimed, run)
        self._spent[claim] = spend_of(claimed)
        return claimed

    def charge(self, run: Run, model: str, usage: Usage) -> Run:
        """The Run with one model call charged on top of all its execution spent so far.
        Raises LoopBudgetExceeded as `cost.charge` does."""
        spend = self._spent.get(run.claim or "", spend_of(run))
        spend = add_call(self._harness, spend, model, usage)
        if run.claim in self._spent:
            self._spent[run.claim] = spend
        return with_spend(run, spend)

    def charge_to(self, run: Run) -> Charge:
        """A Charge for model calls made for `run` without it in hand; `settled` shows them.
        It never raises (`add_call` aside)."""
        claim = run.claim or ""

        def charge(model: str, usage: Usage) -> None:
            if claim in self._spent:
                spent = add_call(self._harness, self._spent[claim], model, usage, aside=True)
                self._spent[claim] = spent

        return charge

    def settled(self, run: Run) -> Run:
        """The Run showing everything its execution has spent."""
        spend = self._spent.get(run.claim or "")
        return run if spend is None else with_spend(run, spend)

    async def save(self, run: Run, **changes: Any) -> Run:
        """Save the Run, all it spent included, with these changes; RunClaimLost if another
        execution holds it."""
        run = self.settled(run).model_copy(update={**changes, "updated_at": self._clock()})
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
        The Run is saved with all this execution spent, the redaction of `exc` included.
        """
        run = await self._store.get_run(run_id)
        if run.claim != claim:
            return
        message = await self._redactor.redact(str(exc), self.charge_to(run))  # tool error text
        try:
            run = await self.save(run, status=RunStatus.failed)
        except RunClaimLost:
            log_event("run.fail.skipped", logging.WARNING, error=type(exc).__name__,
                      reason="claim_lost")  # fmt: skip
            return
        detail = {"error": type(exc).__name__, "message": message, "cursor": run.cursor}
        await self.audit(run, "run.failed", detail)
        log_event("run.failed", logging.WARNING, error=type(exc).__name__, cursor=run.cursor)
