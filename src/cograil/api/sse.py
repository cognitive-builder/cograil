"""Server-sent events for a piece of work that must outlive the connection.

`stream_events` runs the work as its own task and relays what it emits as SSE frames. When the
client goes away the stream stops, the task does not: a Run is never cut off half way and left
"running" (a Run is closed by its Runner, which fails closed, not by a dropped connection).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from cograil.errors import CograilError
from cograil.observability import log_event

type Emit = Callable[[str, dict[str, Any]], None]
type Work = Callable[[Emit], Awaitable[None]]


def frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _guarded(work: Work, emit: Emit, finish: Callable[[], None]) -> None:
    try:
        await work(emit)
    except CograilError as exc:
        emit("error", {"type": type(exc).__name__, "message": str(exc)})
    except Exception as exc:  # the stream must end whatever the work did; details stay in logs
        log_event("api.stream_failed", logging.ERROR, error=type(exc).__name__, detail=str(exc))
        emit("error", {"type": "InternalError", "message": "the request could not be completed"})
    finally:
        finish()


async def stream_events(work: Work, tasks: set[asyncio.Task[None]]) -> AsyncIterator[str]:
    """SSE frames for what `work` emits, ending with its last event. `tasks` keeps the task
    alive until it finishes."""
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    emit: Emit = lambda event, data: queue.put_nowait((event, data))  # noqa: E731
    task = asyncio.create_task(_guarded(work, emit, lambda: queue.put_nowait(None)))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    while (item := await queue.get()) is not None:
        yield frame(*item)
