"""MCP surface: server construction and tool registration.

Tools are registered by ``register_*`` functions, one per group, each taking the
server plus whatever it needs. P2 registers the Session lifecycle and the read-only
Exchange tools; Runs and Timelines join in P4.
"""

from mcp.server.fastmcp import FastMCP

from https_proxy_mcp.server.exchange_tools import register_exchange_tools
from https_proxy_mcp.server.session_tools import register_session_tools
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.server.trust_tools import register_trust_tools

SERVER_NAME = "https-proxy-mcp"

INSTRUCTIONS = """\
Captures the HTTPS traffic of an app under test and interleaves it with \
end-to-end test steps, so a failure can be diagnosed by cause rather than symptom.

Call start_capture before anything else; every other tool needs a running Session.

Two habits matter. Drill down rather than dumping: list_exchanges gives compact rows and \
cannot return bodies, so pick one and call get_exchange, then get_exchange_body only if you \
still need the bytes. And prefer wait_for_exchanges over polling tail_exchanges — "do this, \
then wait for that request" is one blocking call.

If a result is empty, call get_status before concluding there was no traffic. It tells you \
whether capture is running at all and how many Exchanges have been seen.

Secrets are replaced with a stable <redacted:sha256:...> digest. The same secret always \
renders to the same marker, so you can tell whether a token changed between requests without \
ever seeing it. Ask for include_secrets only when the value itself is the question.\
"""


def build_server(state: CaptureState | None = None) -> FastMCP:
    """Construct the server with its tool groups registered.

    Kept separate from :func:`main` so tests can drive the tool surface without starting a
    transport, and so a test can supply a :class:`CaptureState` of its own.
    """
    server = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)
    capture_state = state if state is not None else CaptureState()

    register_session_tools(server, capture_state)
    register_trust_tools(server, capture_state)
    register_exchange_tools(server, capture_state)
    return server


def main() -> None:
    """Entry point for the ``https-proxy-mcp`` script. Serves over stdio."""
    build_server().run(transport="stdio")
