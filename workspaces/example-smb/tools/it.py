"""Deterministic in-memory IT access register for the example workspace.

alice, bob and carol each hold a few systems. request_access records a request once:
it is idempotent on (employee, request_id), so a replay by the same employee returns
the first result. A system the register does not know is refused, and so is a request
for access the employee already holds.
The tables below are module state: a Run's registry loads this file fresh, so every
Run starts from the seed tables and the tools of one Run share one copy.
"""

from __future__ import annotations

from typing import Any

from cograil.errors import ToolExecutionError

SYSTEMS: frozenset[str] = frozenset({"crm", "payroll", "wiki", "vpn"})
ACCESS: dict[str, dict[str, str]] = {
    "alice": {"wiki": "read", "vpn": "user"},
    "bob": {"wiki": "write", "vpn": "user", "crm": "read"},
    "carol": {"wiki": "write", "vpn": "user", "payroll": "read"},
}
_REQUESTED: dict[tuple[str, str], dict[str, Any]] = {}


def get_access(employee: str) -> dict[str, str]:
    """The level the employee holds on each system today."""
    access = ACCESS.get(employee)
    if access is None:
        raise ToolExecutionError(f"unknown employee {employee!r}")
    return dict(access)


def request_access(
    employee: str, system: str, access_level: str, request_id: str
) -> dict[str, Any]:
    """Record an access request once; a replay on (employee, request_id) returns the first."""
    key = (employee, request_id)
    if key in _REQUESTED:
        return _REQUESTED[key]
    held = get_access(employee)
    if system not in SYSTEMS:
        raise ToolExecutionError(f"unknown system {system!r}")
    if held.get(system) == access_level:
        raise ToolExecutionError(f"{employee} already holds {access_level} on {system}")
    result = {
        "request_id": request_id,
        "employee": employee,
        "system": system,
        "access_level": access_level,
        "status": "submitted",
    }
    _REQUESTED[key] = result
    return result
