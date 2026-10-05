"""Service limits (issue #37): a per-principal rate limit on POST /chat and a request body cap.

- COGRAIL_CHAT_RATE_LIMIT: how many chat messages one principal may send in a window, as
  `<requests>/<seconds>`. The default is `20/60`; `off` turns the limit off. A message over
  the limit gets a 429 with a Retry-After header, before anything is routed or run.
- Every request body is capped at MAX_BODY_BYTES. A larger one gets a 413 before it is read
  whole, whether it declares its length or streams in chunks.

The limiter counts in this process. Behind several workers or replicas each one counts on its
own, so the effective limit is the limit times the number of processes.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cograil.api.deps import CurrentPrincipal
from cograil.errors import LimitNotConfigured
from cograil.observability import log_event

MAX_BODY_BYTES = 1024 * 1024
RATE_LIMIT_ENV = "COGRAIL_CHAT_RATE_LIMIT"


@dataclass(frozen=True)
class RateLimit:
    """At most `requests` in any `window_seconds`."""

    requests: int
    window_seconds: float


DEFAULT_CHAT_RATE = RateLimit(requests=20, window_seconds=60)


def chat_rate_limit(env: Mapping[str, str]) -> RateLimit | None:
    """The chat rate limit COGRAIL_CHAT_RATE_LIMIT asks for; None when it is `off`."""
    raw = env.get(RATE_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_CHAT_RATE
    if raw.lower() == "off":
        return None
    requests, _, seconds = raw.partition("/")
    if not (requests.isdigit() and seconds.isdigit() and int(requests) > 0 and int(seconds) > 0):
        raise LimitNotConfigured(f"{RATE_LIMIT_ENV} must be <requests>/<seconds> or off")
    return RateLimit(requests=int(requests), window_seconds=int(seconds))


class RateLimiter:
    """A sliding window per key: a hit is allowed when fewer than `limit.requests` were allowed
    in the last `limit.window_seconds`. A refused hit is not counted."""

    def __init__(self, limit: RateLimit, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Count a hit for `key`; None when allowed, else the seconds until one would be."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.limit.window_seconds:
            hits.popleft()
        if len(hits) >= self.limit.requests:
            return hits[0] + self.limit.window_seconds - now
        hits.append(now)
        return None


def limit_chat(request: Request, principal: CurrentPrincipal) -> None:
    """Refuse a chat message over the principal's rate limit with a 429."""
    limiter: RateLimiter | None = request.app.state.cograil_chat_limiter
    if limiter is None:
        return
    wait = limiter.hit(principal.id)
    if wait is None:
        return
    log_event("api.rate_limited", logging.WARNING, principal=principal.id, route="/chat")
    raise HTTPException(
        429,
        f"too many messages; at most {limiter.limit.requests} "
        f"in {limiter.limit.window_seconds:g} seconds",
        headers={"Retry-After": str(max(1, math.ceil(wait)))},
    )


class BodySizeLimit:
    """ASGI middleware: a request body over `max_bytes` gets a 413."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:  # a body streamed without a length
                    raise HTTPException(413, self._detail)
            return message

        await self.app(scope, counted, send)

    @property
    def _detail(self) -> str:
        return f"the request body is over {self.max_bytes} bytes"

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": self._detail}, status_code=413)
        await response(scope, receive, send)
