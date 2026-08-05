"""MCP surface: server construction and tool registration.

Tools are registered by ``register_*`` functions, one per group, each taking the
server plus whatever it needs. P0 registers none — the server exists, speaks the
protocol, and advertises an empty tool list.
"""

from mcp.server.fastmcp import FastMCP

SERVER_NAME = "https-proxy-mcp"

INSTRUCTIONS = """\
Captures the HTTPS traffic of an app under test and interleaves it with \
end-to-end test steps, so a failure can be diagnosed by cause rather than symptom.

No tools are registered yet.\
"""


def build_server() -> FastMCP:
    """Construct the server with its tool groups registered.

    Kept separate from :func:`main` so tests can introspect the tool surface
    without starting a transport.
    """
    return FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)


def main() -> None:
    """Entry point for the ``https-proxy-mcp`` script. Serves over stdio."""
    build_server().run(transport="stdio")
