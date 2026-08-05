"""A ProxyRuntime that binds nothing, for tests about the tool surface rather than the proxy.

`CaptureState` takes a ``runtime_factory`` precisely so the read tools can be driven without a
listener. Shared rather than copied per module: two divergent stubs is how a test suite ends up
asserting against behaviour the real runtime no longer has.

Un-prefixed so pytest does not collect it.
"""

from __future__ import annotations

from typing import Any

from https_proxy_mcp.capture import ExchangeStore


class StubRuntime:
    """Reports a port and a host, starts and stops, touches no socket."""

    def __init__(
        self,
        store: ExchangeStore,
        *,
        port: int,
        confdir: Any,
        host: str = "127.0.0.1",
    ) -> None:
        self._store = store
        self._port = port or 51234
        self.host = host
        self.is_running = False

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False
