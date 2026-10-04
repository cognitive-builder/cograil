"""Structured logging. Every event is one JSON line on the `cograil` logger.

The current Run id lives in a context variable and is stamped on every event.
OpenTelemetry spans and cost attributes arrive with their own issue.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from pydantic_core import to_jsonable_python

run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

_logger = logging.getLogger("cograil")


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event; fields must never carry secrets."""
    record: dict[str, Any] = {"event": event, "run_id": run_id.get(), **fields}
    _logger.log(level, json.dumps(to_jsonable_python(record, fallback=str), sort_keys=True))
