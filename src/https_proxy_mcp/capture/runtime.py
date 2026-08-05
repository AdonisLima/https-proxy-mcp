"""Running mitmproxy inside this process, on the server's own event loop.

ADR-0001 chose one process sharing one ExchangeStore, so the addon holds native flow objects
with no IPC and no file round-trip for live queries. This module is where that choice is
cashed in: :class:`DumpMaster` runs as an asyncio task alongside the MCP server, and the
addon it carries writes into the very store the tools read from.

Three details here were found by experiment rather than reading, and each would be a bug if
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
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from https_proxy_mcp.capture.addon import CaptureAddon
from https_proxy_mcp.capture.store import ExchangeStore

#: Loopback only. A proxy reachable from the network would be an open relay.
LISTEN_HOST = "127.0.0.1"

#: How long to wait for the listener to come up before giving up.
START_TIMEOUT_S = 30.0

#: How long to wait for the master task to finish after being asked to stop.
STOP_TIMEOUT_S = 15.0


class ProxyRuntime:
    """An in-process mitmproxy, recording into the store the tool surface reads."""

    def __init__(
        self,
        store: ExchangeStore,
        *,
        port: int,
        confdir: Path | str,
        host: str = LISTEN_HOST,
    ) -> None:
        self._store = store
        self._requested_port = port
        self._confdir = Path(confdir)
        self._host = host
        self._master: DumpMaster | None = None
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

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the proxy and return once it is accepting connections."""
        if self._master is not None:
            raise RuntimeError("the proxy is already running")

        self._confdir.mkdir(parents=True, exist_ok=True)

        # with_termlog and with_dumper off: this process speaks MCP on stdout, and a proxy
        # that logs there would corrupt the protocol framing.
        master = DumpMaster(Options(), with_termlog=False, with_dumper=False)
        master.options.update(
            confdir=str(self._confdir),
            mode=[f"regular@{self._host}:{self._requested_port}"],
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

        if task is not None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT_S):
                    await task
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
