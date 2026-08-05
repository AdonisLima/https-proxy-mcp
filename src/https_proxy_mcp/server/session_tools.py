"""The Session lifecycle tools: start_capture, stop_capture, get_status.

Tool docstrings are prompts, not comments — they are the only thing telling an agent when to
reach for each one, so they say what the tool is *for* rather than restating its name.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets import known_targets


def register_session_tools(server: FastMCP, state: CaptureState) -> None:
    """Register the tools that begin and end a Session."""

    @server.tool()
    async def start_capture(
        target: str = "curl",
        port: int = 8080,
        scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """Begin capturing HTTPS traffic into a new Session.

        Must be called before any other traffic tool. Returns the Session id and the port the
        proxy is listening on — pass ``port=0`` to have one chosen and reported back.

        Targets available: {targets}. With the ``curl`` target nothing is routed for you:
        send traffic through the proxy yourself with ``curl -x http://127.0.0.1:<port> ...``.

        ``scope`` narrows what later calls *report*, never what is captured. Everything
        through the proxy is recorded regardless.
        """
        return await state.start(target=target, port=port, scope=scope)

    # Written after the fact so the available targets stay true as P3 adds the simulator.
    start_capture.__doc__ = (start_capture.__doc__ or "").format(targets=", ".join(known_targets()))

    @server.tool()
    async def stop_capture() -> dict[str, Any]:
        """Stop capturing, release the proxy port, and undo any routing that was arranged.

        Reports how many Exchanges were recorded and where the Session directory is. That
        directory holds *unredacted* bodies and stays on disk until retention prunes it.
        """
        return await state.stop()

    @server.tool()
    def get_status() -> dict[str, Any]:
        """Report whether capture is running, and the Session's counts so far.

        Worth calling before concluding that an empty result means "no traffic": it
        distinguishes not recording at all from recording and seeing nothing. ``evicted`` is
        how many Exchanges have aged out of the in-memory buffer.
        """
        return state.status()
