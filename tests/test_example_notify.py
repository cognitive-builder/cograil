"""Example-workspace notify pack: one test per acceptance criterion of issue #84."""

from pathlib import Path

import pytest

from cograil.errors import ToolExecutionError
from cograil.registry import CallContext, build_registry
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
NOTE = {"to": "alice@example.com", "message": "Request req-1 is with bob."}


async def test_send_delivers_in_memory_and_returns_a_deterministic_result(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(load_workspace(EXAMPLE), store, EXAMPLE) as registry:
        first = await registry.invoke("notify.send", NOTE, ctx)
        replay = await registry.invoke("notify.send", NOTE, ctx)
        other = await registry.invoke("notify.send", NOTE | {"to": "hr-ops@example.com"}, ctx)
    assert (first["to"], first["message"], first["status"]) == (
        NOTE["to"],
        NOTE["message"],
        "sent",
    )
    assert replay == first  # the same message twice is one delivery, not two
    assert other["id"] != first["id"]  # a different message is its own delivery


async def test_build_registry_succeeds_over_the_full_example_workspace(
    store: InMemoryRunStore,
) -> None:
    workspace = load_workspace(EXAMPLE)
    built = {tool.name for tool in workspace.tools if tool.kind in ("python", "decision")}
    async with await build_registry(workspace, store, EXAMPLE) as registry:
        assert {tool.name for tool in registry.tools} == built


def test_send_is_declared_as_an_ungated_write_with_both_args() -> None:
    tool = next(t for t in load_workspace(EXAMPLE).tools if t.name == "notify.send")
    assert (tool.kind, tool.scope, tool.confirm_before_write) == ("python", "write", False)
    assert tool.args_schema["required"] == ["to", "message"]


@pytest.mark.parametrize(
    "args",
    [{"to": "  ", "message": "hi"}, {"to": "alice@example.com", "message": ""}],
    ids=["blank-recipient", "blank-body"],
)
async def test_a_blank_recipient_or_body_is_a_tool_execution_error(
    args: dict[str, str], store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(load_workspace(EXAMPLE), store, EXAMPLE) as registry:
        with pytest.raises(ToolExecutionError, match="recipient and a body"):
            await registry.invoke("notify.send", args, ctx)
