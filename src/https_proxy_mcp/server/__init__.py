"""MCP surface: server construction and tool registration.

Tools are registered by ``register_*`` functions, one per group, each taking the
server plus whatever it needs: the Session lifecycle, trust, the read-only Exchange
tools, and Runs and Timelines.
"""

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from https_proxy_mcp.capture.session import default_root
from https_proxy_mcp.server.exchange_tools import register_exchange_tools
from https_proxy_mcp.server.run_tools import register_run_tools
from https_proxy_mcp.server.session_tools import register_session_tools
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.server.trust_tools import register_trust_tools
from https_proxy_mcp.targets.systemproxy import repair_system_proxy, run_command
from https_proxy_mcp.version import resolve_version

SERVER_NAME = "https-proxy-mcp"

INSTRUCTIONS = """\
Captures the HTTPS traffic of an app under test and interleaves it with \
end-to-end test steps, so a failure can be diagnosed by cause rather than symptom.

Call start_capture before anything else; every other tool needs a running Session.

Three calls are the whole path: start_capture, then run_test to run your end-to-end test, \
then get_timeline to read why it failed. get_timeline is the diagnosis — it says what each \
step caused and what was already in flight when the step began, and that second one is \
usually the answer. Driving the app yourself instead of running a test? Wrap each action in \
start_step and end_step and you get the same Timeline.

Two habits matter. Drill down rather than dumping: list_exchanges gives compact rows and \
cannot return bodies, so pick one and call get_exchange, then get_exchange_body only if you \
still need the bytes. And prefer wait_for_exchanges over polling tail_exchanges — "do this, \
then wait for that request" is one blocking call.

If a result is empty, call diagnose_capture before concluding there was no traffic. Nothing \
reaching the proxy, an untrusted authority, certificate pinning and an over-narrow scope all \
look identical from the outside, and it is the only tool that tells them apart.

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
    # FastMCP takes no `version`, but the low-level server it wraps reads its own `version`
    # attribute when it builds the handshake — and falls back to the MCP SDK's version when that
    # is None. So this assignment is the whole difference between a client being told which build
    # of this server it is talking to and being told which SDK we happen to depend on.
    server._mcp_server.version = resolve_version()

    capture_state = state if state is not None else CaptureState()

    register_session_tools(server, capture_state)
    register_trust_tools(server, capture_state)
    register_exchange_tools(server, capture_state)
    register_run_tools(server, capture_state)
    return server


def repair_before_serving(
    root: Path | str | None = None,
    *,
    runner: Any = run_command,
) -> str | None:
    """Layer 3 of the teardown guarantee: restore a sentinel a dead predecessor left behind.

    Runs before the server does anything else, which is what makes ``kill -9`` survivable —
    handlers cover an orderly exit and nothing else. Running it *first* means the machine's
    networking is fixed even if this start then fails for an unrelated reason.

    What the ordering no longer buys is the old justification for ``route_traffic``'s refusal.
    This used to restore and delete *every* record it found, so any sentinel present afterwards
    really did belong to this process. It now deliberately leaves a live foreign owner's record
    in place, so a sentinel present later may well belong to another process — which is why the
    Session that would trip over it is refused at ``prepare()``, naming the holder, rather than at
    ``redirect()``, which can only say that a file is in the way.

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
