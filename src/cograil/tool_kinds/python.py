"""kind=python: the Tool name is a dotted path to a function.

`hris.get_balance` resolves to `get_balance` in the workspace's `tools/hris.py`, and
otherwise to `cograil.tools.hris.get_balance`. Every part of the name must be a Python
identifier, so a name can never reach outside those two places.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

from cograil.domain import Tool
from cograil.errors import ToolConfigError
from cograil.tool_kinds import Invoke

_PACKAGE = "cograil.tools"


class PythonResolver:
    """Resolves python Tools; tools sharing a module share one loaded copy of it."""

    def __init__(self, workspace_root: Path | None) -> None:
        self._root = workspace_root
        self._modules: dict[str, ModuleType | None] = {}

    def resolve(self, tool: Tool) -> Invoke:
        parts = tool.name.split(".")
        if len(parts) < 2 or not all(part.isidentifier() for part in parts):
            raise ToolConfigError(f"{tool.name}: a python Tool name must be module.function")
        module_path, attr = parts[:-1], parts[-1]
        module = self._workspace_module(module_path) or _package_module(module_path)
        if module is None:
            raise ToolConfigError(f"{tool.name}: not found in the workspace or {_PACKAGE}")
        func = getattr(module, attr, None)
        if not callable(func):
            raise ToolConfigError(f"{tool.name}: {module.__name__}.{attr} is not callable")
        return _as_invoke(func)

    def _workspace_module(self, module_path: list[str]) -> ModuleType | None:
        if self._root is None:
            return None
        key = ".".join(module_path)
        if key not in self._modules:
            self._modules[key] = _load_file(self._root / "tools", module_path)
        return self._modules[key]


def _load_file(tools_dir: Path, module_path: list[str]) -> ModuleType | None:
    file = tools_dir.joinpath(*module_path).with_suffix(".py")
    if not file.is_file():
        return None
    if not file.resolve().is_relative_to(tools_dir.resolve()):
        raise ToolConfigError(f"{file}: resolves outside the workspace tools folder")
    name = "cograil_workspace_tools." + ".".join(module_path)
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ToolConfigError(f"{file}: cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolConfigError(f"{file}: failed to load: {exc}") from exc
    return module


def _package_module(module_path: list[str]) -> ModuleType | None:
    name = ".".join([_PACKAGE, *module_path])
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and (name + ".").startswith(exc.name + "."):
            return None
        raise ToolConfigError(f"{name}: failed to import: {exc}") from exc


def _as_invoke(func: Any) -> Invoke:
    if inspect.iscoroutinefunction(func):

        async def call_async(args: dict[str, Any]) -> Any:
            return await func(**args)

        return call_async

    async def call_sync(args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(func, **args)

    return call_sync
