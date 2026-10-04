"""Tool kinds the registry can build: python, rest and mcp.

Each kind turns a Tool into an Invoke: an async callable taking validated arguments. A kind
whose results depend on who asks (knowledge, directory) is an EntitledInvoke instead: it also
gets the calling principal's groups, from the Run and never from the model's arguments, so
it can filter by them before it retrieves anything (product rule 3).
"""

from collections.abc import Awaitable, Callable
from typing import Any

Invoke = Callable[[dict[str, Any]], Awaitable[Any]]
EntitledInvoke = Callable[[dict[str, Any], tuple[str, ...]], Awaitable[Any]]
