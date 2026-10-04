"""Tool kinds the registry can build: python, rest and mcp.

Each kind turns a Tool into an Invoke: an async callable taking validated arguments.
"""

from collections.abc import Awaitable, Callable
from typing import Any

Invoke = Callable[[dict[str, Any]], Awaitable[Any]]
