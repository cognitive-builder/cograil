"""The sweep that expires overdue Approvals on a timer, so no request has to.

An Approval past its `expires_at` escalates the Run to the Colleague's escalation_contact. That
used to happen only when someone opened the approval link, which made a GET change state and
left an Approval nobody opened `awaiting_approval` forever (issue #282). `sweep_forever` calls
the sweep once at startup, which catches whatever fell due while the service was down, and then
every `SWEEP_INTERVAL_SECONDS`. A sweep that fails is logged and the next tick still runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from cograil.observability import log_event

SWEEP_INTERVAL_SECONDS = 60

type Sweep = Callable[[], Awaitable[list[str]]]


async def sweep_forever(sweep: Sweep, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    """Run `sweep` now and every `interval` seconds until cancelled.

    The broad `except Exception` is deliberate: this is a loop that must survive. Nothing else
    expires an Approval, so whatever one sweep raises is logged and the next tick runs."""
    while True:
        try:
            expired = await sweep()
        except Exception as exc:  # a failed sweep must not end the loop: nothing else expires them
            log_event("approval.sweep_failed", logging.ERROR, error=f"{type(exc).__name__}: {exc}")
        else:
            if expired:
                log_event("approval.sweep_expired", count=len(expired))
        await asyncio.sleep(interval)
