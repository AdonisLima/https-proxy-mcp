"""P0 acceptance: a real MCP client completes a handshake over stdio against the built server.

This is the criterion from PLAN.md P0 — not that the object constructs, but that the
protocol round-trips. It exercises the installed entry point, so a broken
``[project.scripts]`` wiring fails here rather than in front of a user.
"""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_handshake_reports_server_name_and_no_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from https_proxy_mcp import main; main()"],
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        result = await session.initialize()
        assert result.serverInfo.name == "https-proxy-mcp"

        tools = await session.list_tools()
        assert tools.tools == []
