"""A real MCP client completes a handshake over stdio and sees the tool surface.

The criterion from PLAN.md P0 — not that the object constructs, but that the protocol
round-trips. It exercises the installed entry point, so broken ``[project.scripts]`` wiring
fails here rather than in front of a user.

Since P2 it also pins the advertised tool surface, which is the contract a client sees before
it calls anything.
"""

import asyncio
import sys
import urllib.request

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from https_proxy_mcp import __version__
from https_proxy_mcp.capture.session import ROOT_ENV
from test_server import REGISTERED_TOOLS

SERVER_ARGS = ["-c", "from https_proxy_mcp import main; main()"]


async def test_stdio_handshake_reports_the_server_and_its_tools():
    params = StdioServerParameters(command=sys.executable, args=SERVER_ARGS)

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        result = await session.initialize()
        assert result.serverInfo.name == "https-proxy-mcp"
        # What a client actually receives, across the transport rather than from the object.
        assert result.serverInfo.version == __version__

        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == REGISTERED_TOOLS


def _get(url: str) -> int:
    """A synchronous probe, kept off the event loop's thread with ``asyncio.to_thread``.

    A blocking GET on the loop's thread would starve the very web stack being probed — the
    same trap ``capture/CLAUDE.md`` records for subprocesses.
    """
    with urllib.request.urlopen(url, timeout=10) as response:
        return int(response.status)


@pytest.mark.integration
async def test_the_protocol_pipe_stays_intact_with_the_human_view_on(tmp_path):
    """The Human View runs a web server inside the same process and event loop as the
    JSON-RPC pipe. If it ever wrote to stdout or starved the loop, the calls below are what
    break — which makes this the existential check that enabling the view cannot wedge the
    session that asked for it.

    Each call is awaited in turn rather than written in a batch: closing stdin on in-flight
    responses loses them (see ``server/CLAUDE.md``), which reads exactly like a
    dropped-response bug.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=SERVER_ARGS,
        # ``stdio_client`` forwards nothing but what is named here, and Sessions hold
        # unredacted bodies — a test must never write them under a real home.
        env={"PATH": "/usr/bin:/bin", ROOT_ENV: str(tmp_path / "root")},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        started = await session.call_tool(
            "start_capture", {"target": "curl", "port": 0, "web_ui": True}
        )
        assert not started.isError, started.content
        web_url = (started.structuredContent or {})["web_url"]
        assert web_url is not None

        # The view answers while the protocol keeps round-tripping: both halves, or the probe
        # proves nothing about the web stack sharing the loop.
        assert await asyncio.to_thread(_get, web_url) == 200

        reporting = await session.call_tool("get_status", {})
        assert not reporting.isError, reporting.content
        assert (reporting.structuredContent or {})["web_url"] == web_url

        stopped = await session.call_tool("stop_capture", {})
        assert not stopped.isError, stopped.content
