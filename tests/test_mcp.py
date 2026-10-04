"""kind=mcp tests for issue #8: a local MCP server over stdio and in memory; no network."""

import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from cograil.domain import McpServer, Tool, Workspace
from cograil.errors import ToolArgumentError, ToolConfigError, ToolExecutionError
from cograil.registry import CallContext, build_registry
from cograil.store import InMemoryRunStore
from cograil.tool_kinds.mcp import default_mcp_client

SERVER = Path(__file__).parent / "fixtures" / "mcp" / "calendar_server.py"
STDIO = McpServer(transport="stdio", command=sys.executable, args=[str(SERVER)])


def calendar(server: McpServer = STDIO) -> Workspace:
    entry = Tool(name="calendar", kind="mcp", scope="write", mcp=server)
    return Workspace(name="ws", colleagues=[], protocols=[], tools=[entry])


def in_memory(server: McpServer) -> Client[Any]:
    return Client(runpy.run_path(str(SERVER))["server"])


@pytest.mark.parametrize("factory", [default_mcp_client, in_memory], ids=["stdio", "memory"])
async def test_mcp_tools_are_listed_under_the_prefix_and_invoked(
    factory: Any, store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(calendar(), store, mcp_client=factory) as registry:
        assert {t.name for t in registry.tools} == {"calendar.add_days", "calendar.fail"}
        tool = registry.get("calendar.add_days")
        assert (tool.scope, tool.confirm_before_write) == ("write", True)
        assert tool.args_schema["required"] == ["start", "days"]
        assert await registry.invoke(tool.name, {"start": 3, "days": 2}, ctx) == {"result": 5}
        with pytest.raises(ToolArgumentError):
            await registry.invoke(tool.name, {"start": "3"}, ctx)
        with pytest.raises(ToolExecutionError, match="calendar offline"):
            await registry.invoke("calendar.fail", {}, ctx)
    assert len(await store.list_tool_calls("r1")) == 3


def test_http_server_uses_streamable_http_transport() -> None:
    client = default_mcp_client(McpServer(transport="http", url="https://mcp.test/mcp"))
    assert isinstance(client.transport, StreamableHttpTransport)
    assert client.transport.url == "https://mcp.test/mcp"


async def test_unreachable_server_is_a_config_error(store: InMemoryRunStore) -> None:
    broken = McpServer(transport="stdio", command=sys.executable, args=["-c", "raise SystemExit"])
    with pytest.raises(ToolConfigError):
        await build_registry(calendar(broken), store)
