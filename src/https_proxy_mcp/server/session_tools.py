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
        udid: str | None = None,
        passthrough: bool = True,
        lens: str | None = None,
    ) -> dict[str, Any]:
        """Begin capturing HTTPS traffic into a new Session.

        Must be called before any other traffic tool. Returns the Session id and the port the
        proxy is listening on — pass ``port=0`` to have one chosen and reported back.

        Targets available: {targets}. With the ``curl`` target nothing is routed for you:
        send traffic through the proxy yourself with ``curl -x http://127.0.0.1:<port> ...``.

        With ``ios-simulator`` the Mac's system proxy is redirected and the Interception
        Authority is installed into the device, both undone by ``stop_capture``. The simulator
        must already be booted. ``udid`` names which one — required when more than one is
        running, since guessing would capture the wrong app and report success.

        ``scope`` names the hosts you care about. It narrows what later calls *report*, and — with
        ``passthrough`` left on — it is also the set of hosts whose HTTPS gets decrypted: every
        other HTTPS connection on the machine is forwarded without being opened and arrives with
        the real certificate, so the developer's browser and any other simulator carry on
        untouched. Name no ``scope`` and everything through the proxy is intercepted and recorded,
        which is what exploratory capture wants.

        Passthrough covers HTTPS only. Plain HTTP is intercepted from every host whatever the
        ``scope`` says — a plain-HTTP request has no destination to test until the proxy has read
        it — so a plain-HTTP host outside the ``scope`` is hidden from listings rather than
        missing, and ``get_exchange`` still fetches it by id.

        ``passthrough=False`` decrypts everything despite a ``scope`` — reach for it when a
        ``scope`` turns out to be too narrow, since an HTTPS host outside it was never decrypted
        and so cannot be waited for or fetched. The result says which way it went.

        ``lens`` narrows what is *reported* by anything else the expression can say — the same
        filter language ``search_exchanges`` takes. Unlike a ``scope`` it is set once and applies
        everywhere, including ``get_timeline`` and ``wait_for_exchanges``, so it cannot be
        forgotten on the one call that mattered. Reach for it when a host cannot tell the traffic
        apart: **two simulators running the same app against the same hosts** are one machine-wide
        proxy away from each other, nothing at the proxy says which sent a request, and a debug
        build that stamps a header on its own requests is what makes them separable —
        ``lens='~hq "X-Sim: sidekick"'``. **Quote a value containing a space**: unquoted, the
        expression still parses and then matches nothing at all. ``get_exchange`` stays exempt,
        because an id came from somewhere.

        If nothing appears afterwards, call ``diagnose_capture`` rather than guessing: it tells
        an untrusted authority apart from certificate pinning apart from an app that simply has
        not made a request yet.
        """
        return await state.start(
            target=target,
            port=port,
            scope=scope,
            udid=udid,
            passthrough=passthrough,
            lens=lens,
        )

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
    def diagnose_capture() -> dict[str, Any]:
        """Explain why capture is or is not working, and what to do about it.

        **Call this before concluding that an empty result means there was no traffic.** Six
        verdicts, and everything but ``ok`` looks identical from the outside. This is the only tool
        that tells them apart:

        - ``proxy_held_elsewhere`` — the machine's proxy is redirected by another
          https-proxy-mcp process that is still running, so its traffic is going there and not
          here. Says which pid, and which signal makes it release the machine.
        - ``no_connect`` — nothing reached the proxy. Routing is not in place, the simulator is
          not booted, or the app has not made a request yet.
        - ``untrusted_authority`` — the device does not trust the Interception Authority, so
          every HTTPS connection fails before a request exists. Fixed by ``install_authority``.
        - ``tls_failed`` — handshakes are failing while the authority is trusted. Certificate
          pinning is the leading candidate, but the count is machine-wide: every client reaches
          the proxy over loopback, so another simulator or a browser on this Mac talking to an
          in-scope host is counted too, and fails identically.
        - ``out_of_scope`` — traffic was captured and the Scope or denylist reported none of it.
        - ``ok`` — Exchanges are arriving.

        ``observed`` carries the counts the verdict was reached from, so the reasoning can be
        checked rather than taken on faith. When it reports ``passthrough``, an empty result
        proves nothing about an HTTPS host outside the ``scope``: that traffic was never
        decrypted. Plain HTTP is captured regardless, so for that an empty listing really does
        mean the request was hidden or never made.
        """
        return state.diagnose()

    @server.tool()
    def get_status() -> dict[str, Any]:
        """Report whether capture is running, the Session's counts, and the machine's proxy state.

        Worth calling before concluding that an empty result means "no traffic": it
        distinguishes not recording at all from recording and seeing nothing. ``evicted`` is
        how many Exchanges have aged out of the in-memory buffer.

        ``proxy`` answers "is my machine still routed through a capture port?" whether or not
        anything is recording here, because that is read from disk rather than from this Session.
        Its ``owner`` names the process that redirected it and says whether that is this one —
        which is the field to act on, since ``stop_capture`` puts back a redirect this process made
        and nothing else. For someone else's, the detail says which signal releases it.
        """
        return state.status()
