"""Deterministic tools for the two-gate API tests."""

from typing import Any


def lookup(key: str) -> dict[str, str]:
    return {"key": key, "value": "42"}


def record_a(item: str) -> dict[str, Any]:
    return {"recorded": item}


def record_b(item: str) -> dict[str, Any]:
    return {"recorded": item}
