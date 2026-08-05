"""The server's shape: identity, the registered tool surface, and its self-description.

The empty-tool-list assertion that stood here through P0 has done its job and is gone — it
was written to fail loudly at exactly this point. What replaces it is the P2 surface, plus two
structural guards that are cheap to check here and expensive to notice broken anywhere else.
"""

import pytest

from https_proxy_mcp import build_server
from https_proxy_mcp.server import SERVER_NAME

#: Every tool P2 ships. Trust tools arrive in P3; Runs and Timelines in P4.
#: Every tool the v1 read-only surface registers. Asserted exactly rather than by subset, so
#: adding one is a deliberate act and removing one cannot pass unnoticed.
REGISTERED_TOOLS = {
    "start_capture",
    "stop_capture",
    "get_status",
    "diagnose_capture",
    "get_trust_status",
    "install_authority",
    "install_authority_system",
    "list_exchanges",
    "get_exchange",
    "get_exchange_body",
    "search_exchanges",
    "tail_exchanges",
    "wait_for_exchanges",
    "export_curl",
}


@pytest.fixture
async def tools():
    return await build_server().list_tools()


def test_server_builds_with_expected_identity():
    assert build_server().name == SERVER_NAME


async def test_the_tool_surface_is_registered(tools):
    assert {tool.name for tool in tools} == REGISTERED_TOOLS


async def test_every_tool_describes_when_to_use_it(tools):
    """A tool with no description is a tool an agent has to guess at."""
    undescribed = [tool.name for tool in tools if not (tool.description or "").strip()]

    assert undescribed == []


async def test_listing_exchanges_has_no_parameter_that_could_return_a_body(tools):
    """Progressive disclosure enforced at the schema, not by asking nicely.

    ``list_exchanges`` must offer no way to ask for body content, so an agent cannot flood its
    own context in a single call (SPEC.md). Adding such a parameter should fail here.
    """
    listing = next(tool for tool in tools if tool.name == "list_exchanges")

    parameters = set(listing.inputSchema["properties"])

    assert not {name for name in parameters if "body" in name or "secret" in name}


async def test_the_available_targets_are_named_in_the_start_tool():
    """Written in at registration, so it cannot drift from the registry as P3 adds targets."""
    tools = await build_server().list_tools()
    start = next(tool for tool in tools if tool.name == "start_capture")

    assert "curl" in (start.description or "")


async def test_the_instructions_tell_a_client_where_to_start():
    assert "start_capture" in (build_server().instructions or "")


async def test_no_resources_or_prompts_registered_yet():
    server = build_server()
    assert await server.list_resources() == []
    assert await server.list_prompts() == []
