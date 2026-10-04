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
        intercept_hosts: tuple[str, ...] | None = None,
        web_ui: bool = False,
        web_port: int = 0,
    ) -> None:
        self._store = store
        self._port = port or 51234
        self.host = host
        self.is_running = False
        #: The hosts Passthrough decided to decrypt, or ``None`` for everything. Kept rather than
        #: dropped: it is the only place a Session's Passthrough decision is visible, since nothing
        #: here binds a port to observe it through.
        self.intercept_hosts = intercept_hosts
        # Kept for the same reason as ``intercept_hosts``: what the Session asked for is only
        # visible in what it hands the runtime.
        self.web_ui = web_ui
        self.web_port = web_port

    @property
    def port(self) -> int:
        return self._port

    @property
    def web_url(self) -> str | None:
        """A stand-in URL when the view was asked for, ``None`` otherwise — never raises."""
        if not self.web_ui:
            return None
        return f"http://{self.host}:{self.web_port or 51235}/?token=stub-token"

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False
