"""MCP surface: server construction and tool registration.

Tools are registered by ``register_*`` functions, one per group, each taking the
server plus whatever it needs. P2 registers the Session lifecycle and the read-only
Exchange tools; Runs and Timelines join in P4.
"""

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from https_proxy_mcp.capture.session import default_root
from https_proxy_mcp.server.exchange_tools import register_exchange_tools
from https_proxy_mcp.server.session_tools import register_session_tools
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.server.trust_tools import register_trust_tools
from https_proxy_mcp.targets.systemproxy import repair_system_proxy, run_command

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


def repair_before_serving(
    root: Path | str | None = None,
    *,
    runner: Any = run_command,
) -> str | None:
    """Layer 3 of the teardown guarantee: restore a sentinel a dead predecessor left behind.

    Runs before the server does anything else, which is what makes ``kill -9`` survivable —
    handlers cover an orderly exit and nothing else. Two consequences of running it *first*:
    the machine's networking is fixed even if this start then fails for an unrelated reason, and
    ``route_traffic`` can safely refuse to overwrite a sentinel it finds, because by then any
    sentinel present really does belong to this process.

    Reported on **stderr**. stdout is the MCP transport, and while a stray line there turns out
    not to corrupt framing, an interleaved write inside a JSON-RPC line would.
    """
    message = repair_system_proxy(root if root is not None else default_root(), runner=runner)
    if message is not None:
        print(f"https-proxy-mcp: {message}", file=sys.stderr, flush=True)
    return message


def main() -> None:
    """Entry point for the ``https-proxy-mcp`` script. Serves over stdio."""
    repair_before_serving()
    build_server().run(transport="stdio")
