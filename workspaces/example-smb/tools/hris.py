"""Deterministic in-memory HRIS for the example workspace.

alice, bob and carol each hold a balance per tracked leave type. submit_leave
counts inclusive calendar days, draws the balance down, and is idempotent on
(employee, request_id): a replay by the same employee returns the first result
without deducting again. Leave types that are not tracked, such as unpaid,
submit without touching a balance.
The tables below are module state: a Run's registry loads this file fresh, so
every Run starts from the seed tables and the tools of one Run share one copy.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cograil.errors import ToolExecutionError

BALANCES: dict[str, dict[str, int]] = {
    "alice": {"annual": 25, "sick": 10},
    "bob": {"annual": 20, "sick": 10},
    "carol": {"annual": 15, "sick": 10},
}
MANAGERS: dict[str, str] = {
    "alice": "bob",
    "bob": "carol",
    "carol": "hr-ops@example.com",
}
_SUBMITTED: dict[tuple[str, str], dict[str, Any]] = {}


def get_balance(employee: str) -> dict[str, int]:
    """Days remaining per tracked leave type, as of the last submit."""
    return dict(_balances(employee))


def get_manager(employee: str) -> dict[str, str]:
    """The approving manager for an employee."""
    manager = MANAGERS.get(employee)
    if manager is None:
        raise ToolExecutionError(f"unknown employee {employee!r}")
    return {"employee": employee, "manager": manager}


def submit_leave(
    employee: str, start: str, end: str, leave_type: str, request_id: str
) -> dict[str, Any]:
    """Record a leave once; a replay on (employee, request_id) returns the first result."""
    key = (employee, request_id)
    if key in _SUBMITTED:
        return _SUBMITTED[key]
    balances = _balances(employee)
    days = _inclusive_days(start, end)
    remaining = balances.get(leave_type)
    if remaining is not None and days > remaining:
        raise ToolExecutionError(
            f"insufficient {leave_type} balance for {employee}:"
            f" {days} days requested, {remaining} remaining"
        )
    if remaining is not None:
        balances[leave_type] = remaining - days
    result = {
        "request_id": request_id,
        "employee": employee,
        "leave_type": leave_type,
        "start": start,
        "end": end,
        "days": days,
        "remaining": balances.get(leave_type),
        "status": "submitted",
    }
    _SUBMITTED[key] = result
    return result


def _balances(employee: str) -> dict[str, int]:
    balances = BALANCES.get(employee)
    if balances is None:
        raise ToolExecutionError(f"unknown employee {employee!r}")
    return balances


def _inclusive_days(start: str, end: str) -> int:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ToolExecutionError(f"end {end} is before start {start}")
    return (last - first).days + 1
