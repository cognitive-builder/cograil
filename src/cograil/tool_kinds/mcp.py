"""kind=mcp: one Tool entry names an MCP server; its tools register under that name.

An entry `github` whose server lists `create_issue` yields the Tool `github.create_issue`,
with the server's input schema as args_schema and the entry's scope and gate.
Needs the `mcp` extra (fastmcp). Server output is returned as data, never as instructions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cograil.domain import McpServer, Tool
from cograil.errors import ToolConfigError, ToolExecutionError
from cograil.tool_kinds import Invoke

if TYPE_CHECKING:
    from fastmcp import Client

McpClientFactory = Callable[[McpServer], "Client[Any]"]


def default_mcp_client(server: McpServer) -> Client[Any]:
    """A fastmcp Client over stdio (a local command) or streamable HTTP (a url)."""
    try:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
    except ImportError as exc:
        raise ToolConfigError("kind=mcp needs the mcp extra: pip install cograil[mcp]") from exc
    if server.transport == "stdio" and server.command:
        return Client(StdioTransport(command=server.command, args=server.args))
    if server.transport == "http" and server.url:
        return Client(StreamableHttpTransport(url=server.url))
    raise ToolConfigError(f"MCP server is missing its {server.transport} target")


class McpToolset:
    """The tools of one MCP server, listed once and invoked through one client."""

    def __init__(self, entry: Tool, client: Client[Any]) -> None:
        self._entry = entry
        self._client = client

    async def list_tools(self) -> list[tuple[Tool, Invoke]]:
        try:
            async with self._client:
                listed = await self._client.list_tools()
        except Exception as exc:
            raise ToolConfigError(f"{self._entry.name}: cannot list MCP tools: {exc}") from exc
        return [
            (self._as_tool(t.name, t.description, t.input_schema), self._invoker(t.name))
            for t in listed
        ]

    async def aclose(self) -> None:
        await self._client.close()  # type: ignore[no-untyped-call]

    def _as_tool(self, name: str, description: str | None, schema: dict[str, Any]) -> Tool:
        return self._entry.model_copy(
            update={
                "name": f"{self._entry.name}.{name}",
                "description": description or "",
                "args_schema": schema,
            }
        )

    def _invoker(self, remote: str) -> Invoke:
        full_name = f"{self._entry.name}.{remote}"

        async def call(args: dict[str, Any]) -> Any:
            try:
                async with self._client:
                    result = await self._client.call_tool(remote, args, raise_on_error=False)
            except Exception as exc:
                raise ToolExecutionError(f"{full_name}: MCP call failed: {exc}") from exc
            text = "\n".join(c.text for c in result.content if c.type == "text")
            if result.is_error:
                raise ToolExecutionError(f"{full_name}: {text}")
            return result.structured_content if result.structured_content is not None else text

        return call
