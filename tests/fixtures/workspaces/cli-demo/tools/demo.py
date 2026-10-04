"""Deterministic tools for the CLI tests."""

from typing import Any


def lookup(key: str) -> dict[str, str]:
    return {"key": key, "value": "42"}


def record(item: str) -> dict[str, Any]:
    return {"recorded": item}
