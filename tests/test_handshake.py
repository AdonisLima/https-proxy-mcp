"""A real MCP client completes a handshake over stdio and sees the tool surface.

The criterion from PLAN.md P0 — not that the object constructs, but that the protocol
round-trips. It exercises the installed entry point, so broken ``[project.scripts]`` wiring
fails here rather than in front of a user.

Now also the thing that would catch the worst possible regression in this design: the proxy
shares this process with the server, and stdio transport means one stray byte on stdout
corrupts the framing. A handshake that still works is the proof.
"""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_server import P2_TOOLS

SERVER_ARGS = ["-c", "from https_proxy_mcp import main; main()"]


async def test_stdio_handshake_reports_the_server_and_its_tools():
    params = StdioServerParameters(command=sys.executable, args=SERVER_ARGS)

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        result = await session.initialize()
        assert result.serverInfo.name == "https-proxy-mcp"

        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == P2_TOOLS
