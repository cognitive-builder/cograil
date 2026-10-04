"""Tool registry: resolves a Tool name to an invokable and records every invoke.

`invoke` validates arguments against the Tool's args_schema (ToolArgumentError on
failure), calls the Tool, then records a ToolCall and a `tool.called` AuditEvent through
the RunStore, whether the call succeeded or not. A scope=write Tool also gets a
`tool.started` AuditEvent just before it runs, so a crash mid-write still leaves a record.
Whitelists and gates are the runner's job; the registry only answers "what is this Tool
and what did it do".

A `decision` Tool also gets a `decision.evaluated` AuditEvent naming the table, its version and
the rules that fired (ADR 0009); its ToolCall result is the outcome as plain data.

`build_registry` builds the python, rest, mcp and decision kinds of a Workspace. The knowledge
kind registers itself from `knowledge/tool.py` with `register_entitled`: its invoke also gets
the CallContext's groups, the principal's, which the model cannot set. The directory kind is
not implemented yet.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

import httpx
from jsonschema import Draft202012Validator, SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from cograil.decisions import DecisionOutcome, DecisionTable, table_for, table_name
from cograil.domain import AuditEvent, Tool, ToolCall, Workspace
from cograil.errors import (
    CograilError,
    DecisionError,
    ToolArgumentError,
    ToolConfigError,
    ToolExecutionError,
    ToolNotFound,
)
from cograil.observability import log_event
from cograil.store import RunStore
from cograil.tool_kinds import EntitledInvoke, Invoke
from cograil.tool_kinds.mcp import McpClientFactory, McpToolset, default_mcp_client
from cograil.tool_kinds.python import PythonResolver
from cograil.tool_kinds.rest import OAuthClientCredentials, RestTool

if TYPE_CHECKING:
    from fastmcp import Client

Closer = Callable[[], Awaitable[None]]

_HTTP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CallContext:
    """Who calls a Tool and where: recorded on the ToolCall and the AuditEvent. `groups` are
    the principal's, for the kinds that filter by entitlement; none means nothing is visible."""

    run_id: str
    step: int
    principal_id: str
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    tool: Tool
    invoke: EntitledInvoke
    validator: Validator


class ToolRegistry:
    """Tool name to invokable. Use as an async context manager to close MCP clients."""

    def __init__(self, store: RunStore) -> None:
        self._store = store
        self._entries: dict[str, _Entry] = {}
        self._closers: list[Closer] = []

    def register(self, tool: Tool, invoke: Invoke) -> None:
        async def call(args: dict[str, Any], groups: tuple[str, ...]) -> Any:
            return await invoke(args)

        self.register_entitled(tool, call)

    def register_entitled(self, tool: Tool, invoke: EntitledInvoke) -> None:
        """Register a Tool whose invoke also takes the calling principal's groups."""
        if tool.name in self._entries:
            raise ToolConfigError(f"{tool.name}: registered twice")
        self._entries[tool.name] = _Entry(tool, invoke, _validator(tool))

    def on_close(self, closer: Closer) -> None:
        self._closers.append(closer)

    def get(self, name: str) -> Tool:
        if name not in self._entries:
            raise ToolNotFound(name)
        return self._entries[name].tool

    @property
    def tools(self) -> list[Tool]:
        return [entry.tool for entry in self._entries.values()]

    async def invoke(self, name: str, args: Mapping[str, Any], ctx: CallContext) -> Any:
        """Validate, call and record; raises ToolNotFound, ToolArgumentError or
        ToolExecutionError (or a typed error the Tool raised itself)."""
        call = ToolCall(step=ctx.step, tool=name, args=dict(args), started_at=_now())
        try:
            entry = self._entries.get(name)
            if entry is None:
                raise ToolNotFound(name)
            _validate(entry, args)
            if entry.tool.scope == "write":
                await self._audit(ctx, "tool.started", {"tool": name, "step": ctx.step})
            result = await entry.invoke(copy.deepcopy(dict(args)), ctx.groups)
            if isinstance(result, DecisionOutcome):
                detail = {"tool": name, "step": ctx.step, **result.audit_detail()}
                await self._audit(ctx, "decision.evaluated", detail)
                result = result.as_result()
        except CograilError as exc:
            await self._record(ctx, call, error=str(exc))
            raise
        except Exception as exc:
            error = ToolExecutionError(f"{name}: {type(exc).__name__}: {exc}")
            await self._record(ctx, call, error=str(error))
            raise error from exc
        await self._record(ctx, call, result=result)
        return result

    async def _record(
        self, ctx: CallContext, call: ToolCall, result: Any = None, error: str | None = None
    ) -> None:
        done = call.model_copy(update={"result": result, "error": error, "ended_at": _now()})
        await self._store.record_tool_call(ctx.run_id, done)
        detail = {"tool": call.tool, "step": ctx.step, "error": error}
        await self._audit(ctx, "tool.called", detail)
        log_event("tool.called", tool=call.tool, step=ctx.step, ok=error is None)

    async def _audit(
        self,
        ctx: CallContext,
        kind: Literal["tool.started", "tool.called", "decision.evaluated"],
        detail: dict[str, Any],
    ) -> None:
        await self._store.append_audit_event(
            AuditEvent(
                run_id=ctx.run_id,
                at=_now(),
                principal_id=ctx.principal_id,
                kind=kind,
                detail=detail,
            )
        )

    async def aclose(self) -> None:
        closers, self._closers = self._closers, []
        for closer in reversed(closers):
            try:
                await closer()
            except Exception as exc:  # a broken client must not mask the error that closed it
                log_event("tool.close_failed", logging.WARNING, error=type(exc).__name__)

    async def __aenter__(self) -> ToolRegistry:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()


async def build_registry(
    workspace: Workspace,
    store: RunStore,
    root: Path | None = None,
    *,
    http: httpx.AsyncClient | None = None,
    mcp_client: McpClientFactory = default_mcp_client,
) -> ToolRegistry:
    """Build the python, rest, mcp and decision Tools of a workspace loaded from root."""
    registry = ToolRegistry(store)
    try:
        resolver = PythonResolver(root)
        for tool in workspace.tools:
            if tool.kind == "python":
                registry.register(tool, resolver.resolve(tool))
            elif tool.kind == "decision":
                registry.register(tool, _decision(tool, workspace))
        _add_rest(registry, workspace, http)
        for tool in workspace.tools:
            if tool.kind == "mcp" and tool.mcp is not None:
                await _add_mcp(registry, tool, mcp_client(tool.mcp))
    except BaseException:
        await registry.aclose()
        raise
    return registry


def _decision(tool: Tool, workspace: Workspace) -> DecisionTable:
    try:
        return table_for(workspace.decisions, table_name(tool))
    except DecisionError as exc:
        raise ToolConfigError(f"{tool.name}: {exc}") from exc


def _add_rest(registry: ToolRegistry, workspace: Workspace, http: httpx.AsyncClient | None) -> None:
    rest = [tool for tool in workspace.tools if tool.kind == "rest"]
    if not rest:
        return
    if http is None:
        http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        registry.on_close(http.aclose)
    connections = {c.name: c for c in workspace.connections}
    auths: dict[str, OAuthClientCredentials] = {}
    for tool in rest:
        connection = connections.get(tool.connection or "")
        if connection is None:
            raise ToolConfigError(f"{tool.name}: unknown connection {tool.connection!r}")
        if connection.auth not in ("none", "oauth_client_credentials"):
            raise ToolConfigError(f"{tool.name}: auth {connection.auth} is not supported yet")
        auth = None
        if connection.auth == "oauth_client_credentials":
            auth = auths.setdefault(
                connection.name, OAuthClientCredentials(connection, http, time.monotonic)
            )
        registry.register(tool, RestTool(tool, connection, http, auth))


async def _add_mcp(registry: ToolRegistry, entry: Tool, client: Client[Any]) -> None:
    toolset = McpToolset(entry, client)
    registry.on_close(toolset.aclose)
    for tool, invoke in await toolset.list_tools():
        registry.register(tool, invoke)


def _validator(tool: Tool) -> Validator:
    cls = validator_for(tool.args_schema, default=Draft202012Validator)
    try:
        cls.check_schema(tool.args_schema)
    except SchemaError as exc:
        raise ToolConfigError(f"{tool.name}: invalid args_schema: {exc.message}") from exc
    return cls(tool.args_schema, format_checker=cls.FORMAT_CHECKER)


def _validate(entry: _Entry, args: Mapping[str, Any]) -> None:
    errors = sorted(entry.validator.iter_errors(dict(args)), key=lambda e: _where(e.path))
    if errors:
        problems = "; ".join(f"{_where(e.path)}: {e.message}" for e in errors)
        raise ToolArgumentError(f"{entry.tool.name}: {problems}")


def _where(path: Any) -> str:
    return "/".join(str(part) for part in path) or "(root)"


def _now() -> datetime:
    return datetime.now(UTC)
