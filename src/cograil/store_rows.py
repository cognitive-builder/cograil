"""Row values for PostgresRunStore: domain models to and from the tables in store_tables."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.exc import IntegrityError

from cograil.domain import Approval, AuditEvent, Run, ToolCall


def _json(value: Any) -> Any:
    """Make a value safe for a JSONB column (datetimes, enums, nested models)."""
    return to_jsonable_python(value)


def run_values(run: Run) -> dict[str, Any]:
    values = run.model_dump()
    for name in ("principal", "trigger", "context"):
        values[name] = _json(values[name])
    values["status"] = str(run.status)
    return values


def call_values(run_id: str, call: ToolCall) -> dict[str, Any]:
    values = call.model_dump()
    values["args"] = _json(values["args"])
    values["result"] = _json(values["result"])
    return {"run_id": run_id, **values}


def approval_values(approval: Approval) -> dict[str, Any]:
    values = approval.model_dump()
    values["args"] = _json(values["args"])
    return values


def event_values(event: AuditEvent) -> dict[str, Any]:
    values = event.model_dump()
    values["detail"] = _json(values["detail"])
    return values


def without(row: Mapping[Any, Any], *keys: str) -> dict[str, Any]:
    """A row as a dict, minus storage-only columns the domain model does not carry."""
    return {name: value for name, value in row.items() if name not in keys}


def sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)
