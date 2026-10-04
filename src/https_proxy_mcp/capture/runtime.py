"""Running mitmproxy inside this process, on the server's own event loop.

ADR-0001 chose one process sharing one ExchangeStore, so the addon holds native flow objects
with no IPC and no file round-trip for live queries. This module is where that choice is
cashed in: :class:`DumpMaster` runs as an asyncio task alongside the MCP server, and the
addon it carries writes into the very store the tools read from.

Four details here were found by experiment rather than reading, and each would be a bug if
guessed differently:

* **Clearing the ``mode`` option is what closes the listener.** ``master.shutdown()`` stops
  the master but leaves the port bound, so a stop-then-start on the same port would fail.
* **The proxy is kept silent, because it shares stdout with the protocol.** termlog and
  dumper are both off, and were measured to emit nothing even when a connection fails.
  Worth being precise about the risk: the MCP client parses stdout line by line and skips
  lines it cannot parse, so a stray *whole* line is survivable. What is not survivable is a
  write that interleaves inside a JSON-RPC line — the proxy and the transport hold separate
  buffers on the same file descriptor, so under concurrency that is possible. Silence is the
  cheap way never to find out, and no test can guard it: keep it that way on purpose.
* **Port 0 works and reports back.** ``listen_addrs()`` gives the port actually bound, which
  is what lets a caller ask for "any free port" and still be told which one.
* **The library never stops its web server; the runtime does.** In ``mitmweb`` the process
  exits first, so the tornado HTTP server has no shutdown path at all — ``shutdown()`` only
  signals the master, and the server object is a local nobody keeps. In this long-lived
  process that leaks a listener per Session, so :class:`_WebProxyMaster` keeps the reference
  and ``stop()`` closes it. The subclass replicates the pinned master's startup body on
  purpose; the dependency's upper bound and the web-port release test are the guard.

Passthrough (CONTEXT.md) is the one decision here that is not about lifecycle. ``intercept_hosts``
names the hosts to decrypt, and every other **TLS** connection is forwarded without being opened —
it arrives carrying the real certificate, so a browser or a simulator that never trusted the
Interception Authority is unaffected by a capture it was never part of. Plain HTTP is not covered
and cannot be: ``next_layer.py``'s ``_ignore_connection`` answers ``False`` when it has no host
name to test, and a plain-HTTP proxy request does not name its destination until the request line
is parsed, which is after the layer has been chosen. Only a CONNECT authority and a TLS SNI arrive
early enough. Measured on a real capture, where every recorded Exchange turned out to be
plain-HTTP bundler traffic from a host the Scope never named. ``None`` means intercept
everything, which is mitmproxy's own default and this project's only behaviour before Passthrough
existed. The patterns are rendered by :mod:`~https_proxy_mcp.capture.hostsets` from the same
definition the Scope predicate answers from; nothing here writes one.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from pathlib import Path

import tornado.httpserver
import tornado.ioloop
from mitmproxy.master import Master
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster
from mitmproxy.tools.web.master import WebMaster

from https_proxy_mcp.capture.addon import CaptureAddon
from https_proxy_mcp.capture.hostsets import allow_host_patterns
from https_proxy_mcp.capture.store import ExchangeStore

#: Loopback only. A proxy reachable from the network would be an open relay.
LISTEN_HOST = "127.0.0.1"

#: How long to wait for the listener to come up before giving up.
START_TIMEOUT_S = 30.0

#: How long to wait for the master task to finish after being asked to stop.
STOP_TIMEOUT_S = 15.0


def _free_loopback_port() -> int:
    """A port that was free a moment ago.

    tornado would bind an ephemeral port itself, but mitmproxy never reads it back, so the
    reported URL would name port 0. The bind-close race is accepted: on losing it the listen
    error surfaces and ``start()``'s half-started cleanup runs.
    """
    with socket.socket() as probe:
        probe.bind((LISTEN_HOST, 0))
        return int(probe.getsockname()[1])


def _bind_web_sockets(port: int, host: str) -> list[socket.socket]:
    """tornado's bind loop, with the leak on its failure path closed.

    ``TCPServer.listen`` delegates to ``bind_sockets``, and a refused bind there — the port is
    already taken, which a Human View start is *expected* to survive — re-raises without closing
    the socket it had just opened, nor any it had already bound. That is one leaked fd per failed
    start, collected whenever the allocator feels like it, so pytest's error-first warning policy
    would report it against whatever innocent test happened to be running. Bound here instead, so
    the failure path closes what it opened.
    """
    sockets: list[socket.socket] = []
    try:
        seen: set[tuple[int, tuple[object, ...]]] = set()
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE
        ):
            if (family, sockaddr) in seen:
                continue
            seen.add((family, sockaddr))
            sock = socket.socket(family, socktype, proto)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setblocking(False)
                sock.bind(sockaddr)
                sock.listen()
            except BaseException:
                sock.close()
                raise
            sockets.append(sock)
    except BaseException:
        for bound in sockets:
            bound.close()
        raise
    return sockets


class _WebProxyMaster(WebMaster):
    """A WebMaster that keeps the reference mitmproxy drops, so the web server can be stopped.

    ``WebMaster.running()`` binds the tornado HTTP server into a local and nothing ever stops
    it — in ``mitmweb`` the process exits first. A Session ends without this process ending, so
    the server is kept on the instance and ``stop_web()`` closes it. The body below mirrors the
    pinned master's, ending at the grandparent's ``running()``: awaiting
    ``WebMaster.running()`` would listen twice. The listen itself goes through
    :func:`_bind_web_sockets`, because tornado's own bind loop leaks the socket when the bind is
    refused.
    """

    def __init__(self, opts: Options) -> None:
        super().__init__(opts, with_termlog=False)
        self._http_server: tornado.httpserver.HTTPServer | None = None

    async def running(self) -> None:
        tornado.ioloop.IOLoop.current()
        self._http_server = tornado.httpserver.HTTPServer(self.app, max_buffer_size=2**32)  # 4GB
        try:
            self._http_server.add_sockets(
                _bind_web_sockets(self.options.web_port, self.options.web_host)
            )
        except OSError as e:
            # The proxy servers are already bound at this point (Master.run sets them up
            # before running()), and proxyserver only answers a mode change once its
            # RunningHook has fired — which the raise below pre-empts. Close them here or
            # the failed start leaves the proxy port bound behind it.
            await self.proxyserver.servers.update([])
            message = (
                f"Web server failed to listen on"
                f" {self.options.web_host or '*'}:{self.options.web_port} with {e}"
            )
            if e.errno == errno.EADDRINUSE:
                message += (
                    f"\nTry specifying a different port by using"
                    f" `--set web_port={self.options.web_port + 2}`."
                )
            raise OSError(e.errno, message, e.filename) from e
        await Master.running(self)

    def stop_web(self) -> None:
        """Close the web listener. Safe before ``running()`` or after a failed listen."""
        if self._http_server is not None:
            self._http_server.stop()
            self._http_server = None


class ProxyRuntime:
    """An in-process mitmproxy, recording into the store the tool surface reads."""

    def __init__(
        self,
        store: ExchangeStore,
        *,
        port: int,
        confdir: Path | str,
        host: str = LISTEN_HOST,
        intercept_hosts: tuple[str, ...] | None = None,
        web_ui: bool = False,
        web_port: int = 0,
    ) -> None:
        self._store = store
        self._requested_port = port
        self._confdir = Path(confdir)
        self._host = host
        # Rendered once, here, so a pattern cannot be assembled anywhere else. An empty tuple lands
        # in the same place as ``None`` because mitmproxy reads an empty ``allow_hosts`` as "open
        # everything" — naming no hosts to decrypt is not something a caller can express, and
        # `hostsets` leaves that decision to whoever computes the set (`server/state.py`).
        self._allow_hosts = list(allow_host_patterns(intercept_hosts)) if intercept_hosts else []
        self._web_ui = web_ui
        self._requested_web_port = web_port
        self._master: DumpMaster | _WebProxyMaster | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None

    # --- properties -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._master is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the requested one when 0 was asked for."""
        if self._port is None:
            raise RuntimeError("the proxy is not running, so it has no port")
        return self._port

    @property
    def web_url(self) -> str | None:
        """The token-gated URL of the Human View, or None when it was not asked for or is down."""
        master = self._master
        if not isinstance(master, _WebProxyMaster):
            return None
        return master.web_url

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the proxy and return once it is accepting connections."""
        if self._master is not None:
            raise RuntimeError("the proxy is already running")

        self._confdir.mkdir(parents=True, exist_ok=True)

        if self._web_ui:
            master: DumpMaster | _WebProxyMaster = _WebProxyMaster(Options())
            master.options.update(
                confdir=str(self._confdir),
                mode=[f"regular@{self._host}:{self._requested_port}"],
                allow_hosts=self._allow_hosts,
                web_host=LISTEN_HOST,
                web_port=self._requested_web_port or _free_loopback_port(),
                # This server is headless and agent-driven; it must never pop a browser.
                web_open_browser=False,
            )
        else:
            # with_termlog and with_dumper off: this process speaks MCP on stdout, and a proxy
            # that logs there would corrupt the protocol framing.
            master = DumpMaster(Options(), with_termlog=False, with_dumper=False)
            master.options.update(
                confdir=str(self._confdir),
                mode=[f"regular@{self._host}:{self._requested_port}"],
                # Passthrough: empty is mitmproxy's "decrypt everything", which is what an
                # unscoped Session wants. Set unconditionally rather than only when non-empty, so
                # the option a started proxy carries is always this object's own decision.
                allow_hosts=self._allow_hosts,
            )
        master.addons.add(CaptureAddon(self._store))

        self._master = master
        self._task = asyncio.create_task(master.run())

        try:
            self._port = await self._await_listening()
        except BaseException:
            # Never leave a half-started proxy holding a port.
            await self.stop()
            raise

    async def _await_listening(self) -> int:
        """Wait for the listener, and report the port it actually bound."""
        assert self._master is not None
        proxyserver = self._master.addons.get("proxyserver")
        try:
            async with asyncio.timeout(START_TIMEOUT_S):
                while True:
                    if self._task is not None and self._task.done():
                        # Surfaces the real startup failure — a port already in use, say —
                        # instead of timing out with nothing to show for it.
                        self._task.result()
                        raise RuntimeError("the proxy stopped before it began listening")
                    addresses = proxyserver.listen_addrs()
                    if addresses:
                        return int(addresses[0][1])
                    await asyncio.sleep(0.02)
        except TimeoutError:
            raise RuntimeError(
                f"the proxy did not listen on {self._host}:{self._requested_port}"
                f" within {START_TIMEOUT_S:.0f}s"
            ) from None

    async def stop(self) -> None:
        """Stop the proxy and release its port. Safe to call twice, or having never started."""
        master, task = self._master, self._task
        self._master, self._task, self._port = None, None, None

        if master is None:
            return

        # Clearing the mode is what closes the listener; shutdown alone leaves it bound.
        master.options.update(mode=[])
        master.shutdown()
        if isinstance(master, _WebProxyMaster):
            # The library never stops its own web server — in mitmweb the process exits first.
            master.stop_web()

        if task is not None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT_S):
                    await task
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
