"""Registry tests for issue #8: python kind, argument validation, ToolCall recording; the
tool pack version is issue #110."""

import asyncio
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cograil.domain import Tool, Workspace
from cograil.errors import (
    CograilError,
    ToolArgumentError,
    ToolConfigError,
    ToolExecutionError,
    ToolNotFound,
)
from cograil.registry import MAX_TOOL_ARGS_BYTES, CallContext, ToolRegistry, build_registry
from cograil.store import InMemoryRunStore

HRIS = textwrap.dedent(
    """
    BALANCES = {"alice": 10}

    def get_balance(employee):
        return BALANCES[employee]

    async def submit_leave(employee, days):
        BALANCES[employee] -= days
        return {"ok": True}
    """
)
EMPLOYEE = {"type": "object", "properties": {"employee": {"type": "string"}}}


def python_tool(name: str, schema: dict[str, Any] | None = None) -> Tool:
    return Tool(name=name, kind="python", scope="read", args_schema=schema or {})


def workspace(*tools: Tool) -> Workspace:
    return Workspace(name="ws", colleagues=[], protocols=[], tools=list(tools))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "hris.py").write_text(HRIS)
    (tmp_path / "outside.py").write_text("def fn():\n    return 1\n")
    (tmp_path / "tools" / "escape.py").symlink_to(tmp_path / "outside.py")
    return tmp_path


async def test_python_resolves_workspace_module_sync_and_async_sharing_state(
    root: Path, store: InMemoryRunStore, ctx: CallContext
) -> None:
    ws = workspace(python_tool("hris.get_balance"), python_tool("hris.submit_leave"))
    async with await build_registry(ws, store, root) as registry:
        await registry.invoke("hris.submit_leave", {"employee": "alice", "days": 3}, ctx)
        assert await registry.invoke("hris.get_balance", {"employee": "alice"}, ctx) == 7


async def test_python_falls_back_to_cograil_tools(
    monkeypatch: pytest.MonkeyPatch, root: Path, store: InMemoryRunStore, ctx: CallContext
) -> None:
    module = ModuleType("cograil.tools.echo")
    module.say = lambda text: f"echo {text}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cograil.tools.echo", module)
    registry = await build_registry(workspace(python_tool("echo.say")), store, root)
    assert await registry.invoke("echo.say", {"text": "hi"}, ctx) == "echo hi"


def changed(root: Path, ws: Workspace, change: str) -> Workspace:
    """The workspace after `change`: a file in tools/ rewritten, or a Tool's schema edited."""
    if change == "unresolved-module":
        (root / "tools" / "unused.py").write_text("X = 1\n")
    elif change == "resolved-module":
        (root / "tools" / "hris.py").write_text(HRIS + "\nX = 1\n")
    elif change == "tools-yaml":
        return workspace(python_tool("hris.get_balance", EMPLOYEE))
    return ws


@pytest.mark.parametrize(
    ("change", "changes_version"),
    [
        ("unchanged", False),
        ("unresolved-module", False),
        ("resolved-module", True),
        ("tools-yaml", True),
    ],
)
async def test_tool_pack_version_covers_the_tools_and_the_modules_they_resolve(
    root: Path, store: InMemoryRunStore, change: str, changes_version: bool
) -> None:
    ws = workspace(python_tool("hris.get_balance"))
    first = (await build_registry(ws, store, root)).tool_pack_version
    second = (await build_registry(changed(root, ws, change), store, root)).tool_pack_version
    assert len(first) == 64
    assert (first != second) is changes_version


@pytest.mark.parametrize(
    "name",
    ["nodots", "os.path..join", "hris.missing", "nowhere.fn", "hris.__builtins__", "escape.fn"],
)
async def test_python_unresolvable_name_raises(
    name: str, root: Path, store: InMemoryRunStore
) -> None:
    with pytest.raises(ToolConfigError):
        await build_registry(workspace(python_tool(name)), store, root)


@pytest.mark.parametrize(
    "args",
    [{}, {"employee": 7}, {"employee": "alice", "start": "not-a-date"}],
    ids=["missing", "wrong-type", "bad-format"],
)
async def test_invalid_args_raise_before_invoke(
    args: dict[str, Any], store: InMemoryRunStore, ctx: CallContext
) -> None:
    calls: list[dict[str, Any]] = []

    async def invoke(a: dict[str, Any]) -> None:
        calls.append(a)

    schema = {**EMPLOYEE, "required": ["employee"]}
    schema["properties"] = {**EMPLOYEE["properties"], "start": {"type": "string", "format": "date"}}
    registry = ToolRegistry(store)
    registry.register(python_tool("hris.get_balance", schema), invoke)
    with pytest.raises(ToolArgumentError):
        await registry.invoke("hris.get_balance", args, ctx)
    assert calls == []


def test_invalid_args_schema_is_a_config_error(store: InMemoryRunStore) -> None:
    with pytest.raises(ToolConfigError):
        ToolRegistry(store).register(python_tool("a.b", {"type": "nonsense"}), _noop)


async def _noop(args: dict[str, Any]) -> None:
    return None


async def _boom(args: dict[str, Any]) -> None:
    raise RuntimeError("hris down")


@pytest.mark.parametrize(
    ("name", "args", "invoke", "error"),
    [
        ("hris.get_balance", {"employee": "alice"}, _noop, None),
        ("hris.get_balance", {"employee": 1}, _noop, ToolArgumentError),
        ("hris.get_balance", {"employee": "alice"}, _boom, ToolExecutionError),
        ("hris.unknown", {}, _noop, ToolNotFound),
    ],
    ids=["ok", "bad-args", "tool-raises", "unknown-tool"],
)
async def test_every_invoke_records_tool_call_and_audit_event(
    name: str,
    args: dict[str, Any],
    invoke: Any,
    error: type[CograilError] | None,
    store: InMemoryRunStore,
    ctx: CallContext,
) -> None:
    registry = ToolRegistry(store)
    registry.register(python_tool("hris.get_balance", EMPLOYEE), invoke)
    if error is None:
        await registry.invoke(name, args, ctx)
    else:
        with pytest.raises(error):
            await registry.invoke(name, args, ctx)
    [call] = await store.list_tool_calls("r1")
    assert (call.tool, call.step, call.args) == (name, 2, args)
    assert (call.error is None) == (error is None) and call.ended_at is not None
    [event] = await store.list_audit_events("r1")
    assert (event.kind, event.principal_id, event.detail["tool"]) == (
        "tool.called",
        "alice@example.com",
        name,
    )


@pytest.mark.parametrize(
    ("crash", "raised", "kinds"),
    [
        (RuntimeError("hris down"), ToolExecutionError, ["tool.started", "tool.called"]),
        (asyncio.CancelledError(), None, ["tool.started"]),
    ],
    ids=["raises", "cancelled"],
)
async def test_write_tool_that_crashes_mid_call_leaves_tool_started(
    crash: BaseException,
    raised: type[BaseException] | None,
    kinds: list[str],
    store: InMemoryRunStore,
    ctx: CallContext,
) -> None:
    async def invoke(args: dict[str, Any]) -> None:
        raise crash

    registry = ToolRegistry(store)
    registry.register(Tool(name="hris.submit_leave", kind="python", scope="write"), invoke)
    with pytest.raises(raised or type(crash)):
        await registry.invoke("hris.submit_leave", {}, ctx)
    events = await store.list_audit_events("r1")
    assert [e.kind for e in events] == kinds
    assert (events[0].principal_id, events[0].detail) == (
        "alice@example.com",
        {"tool": "hris.submit_leave", "step": 2},
    )


async def test_write_tool_with_invalid_args_never_starts(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    registry = ToolRegistry(store)
    tool = Tool(name="hris.submit_leave", kind="python", scope="write", args_schema=EMPLOYEE)
    registry.register(tool, _noop)
    with pytest.raises(ToolArgumentError):
        await registry.invoke("hris.submit_leave", {"employee": 1}, ctx)
    assert [e.kind for e in await store.list_audit_events("r1")] == ["tool.called"]


def padded(size: int) -> dict[str, Any]:
    """Arguments that are exactly `size` bytes as compact JSON: {"employee":"xx…"}."""
    return {"employee": "x" * (size - len('{"employee":""}'))}


@pytest.mark.parametrize(
    ("size", "refused"),
    [(MAX_TOOL_ARGS_BYTES, False), (MAX_TOOL_ARGS_BYTES + 1, True)],
    ids=["at-cap", "over-cap"],
)
async def test_arguments_over_the_size_cap_are_refused_and_audited(
    size: int, refused: bool, store: InMemoryRunStore, ctx: CallContext
) -> None:
    """Issue #286: refused before the Tool runs, recorded without the arguments."""
    calls: list[dict[str, Any]] = []

    async def invoke(args: dict[str, Any]) -> None:
        calls.append(args)

    registry = ToolRegistry(store)
    tool = Tool(name="hris.submit_leave", kind="python", scope="write", args_schema=EMPLOYEE)
    registry.register(tool, invoke)
    if not refused:
        await registry.invoke("hris.submit_leave", padded(size), ctx)
        assert len(calls) == 1
        return
    with pytest.raises(ToolArgumentError, match=f"{size} bytes"):
        await registry.invoke("hris.submit_leave", padded(size), ctx)
    assert calls == []
    [call] = await store.list_tool_calls("r1")
    assert call.args == {} and "cap" in (call.error or "")
    [event] = await store.list_audit_events("r1")
    assert (event.kind, event.principal_id) == ("tool.called", "alice@example.com")
    assert "cap" in event.detail["error"]


async def test_a_lone_surrogate_is_counted_not_raised(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    """A model can send "\\ud800"; the size check must count it, not raise UnicodeEncodeError."""
    await ToolRegistry(store).refuse_oversized("hris.get_balance", {"employee": "\ud800"}, ctx)
    assert await store.list_audit_events("r1") == []
