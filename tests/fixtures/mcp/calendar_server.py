"""A tiny MCP server for registry tests: run over stdio, or imported for in-memory use."""

from fastmcp import FastMCP

server = FastMCP("calendar")


@server.tool
def add_days(start: int, days: int) -> int:
    """Add days to a day number."""
    return start + days


@server.tool
def fail() -> str:
    """Always fails."""
    raise ValueError("calendar offline")


if __name__ == "__main__":
    server.run(show_banner=False)
