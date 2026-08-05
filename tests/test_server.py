"""P0 acceptance: the server constructs, speaks the protocol, and advertises no tools yet.

The empty-tool-list assertion is deliberate. It is the thing that will change in P2,
and having it fail loudly then is the point.
"""

from https_proxy_mcp import build_server
from https_proxy_mcp.server import SERVER_NAME


def test_server_builds_with_expected_identity():
    server = build_server()
    assert server.name == SERVER_NAME


async def test_no_tools_registered_yet():
    tools = await build_server().list_tools()
    assert tools == []


async def test_no_resources_or_prompts_registered_yet():
    server = build_server()
    assert await server.list_resources() == []
    assert await server.list_prompts() == []
