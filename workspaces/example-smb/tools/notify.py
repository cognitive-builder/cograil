"""Deterministic in-memory notifier for the example workspace.

send records each message in a module-level outbox and returns the same result for
the same message: the id is a digest of the recipient and the body, so a replay
never invents a second one and nothing depends on the clock or the call order.
Nothing leaves the process. The outbox is module state: a Run's registry loads
this file fresh, so every Run starts empty and the tools of one Run share one copy.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cograil.errors import ToolExecutionError

OUTBOX: dict[str, dict[str, Any]] = {}


def send(to: str, message: str) -> dict[str, Any]:
    """Record a message once; a replay of the same message returns the first result."""
    if not to.strip() or not message.strip():
        raise ToolExecutionError("a message needs both a recipient and a body")
    result = {"id": _message_id(to, message), "to": to, "message": message, "status": "sent"}
    return OUTBOX.setdefault(result["id"], result)


def _message_id(to: str, message: str) -> str:
    # the length prefix keeps a recipient containing a newline from colliding with a body
    digest = hashlib.sha256(f"{len(to)}:{to}{message}".encode()).hexdigest()
    return f"msg-{digest[:12]}"
