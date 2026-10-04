"""POST /chat: send a message, get the Run's progress as server-sent events.

Events, each `event: <name>` and a JSON `data:` line:

- `routed`: the Colleague and Protocol the message was routed to (null when none fits).
- `refusal`: the message fits nothing the principal may start; `text` says what they can ask.
- `run`: the Run exists; `run_id` names it for /runs/{id}.
- `progress`: one line per AuditEvent and per finished Step as it is written.
- `done`: the Run stopped (completed, awaiting approval or escalated); the body of RunOutcome.
- `error`: the request failed; `type` is the error class, `message` says why.

The stream ends after `refusal`, `done` or `error`. Closing the connection does not stop a Run.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from cograil.api.auth import current_principal
from cograil.api.deps import CurrentPrincipal, ServicesDep
from cograil.api.schemas import ChatRequest
from cograil.api.sse import Emit, stream_events

router = APIRouter(tags=["chat"], dependencies=[Depends(current_principal)])

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events."}},
)
async def chat(
    body: ChatRequest, principal: CurrentPrincipal, services: ServicesDep
) -> StreamingResponse:
    """Route the message to a Colleague's Protocol and stream the Run's progress."""

    async def work(emit: Emit) -> None:
        await services.chat(principal, body.message, emit)

    return StreamingResponse(
        stream_events(work, services.tasks), media_type="text/event-stream", headers=STREAM_HEADERS
    )
