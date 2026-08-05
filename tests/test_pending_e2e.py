"""A request that never comes back, through a real proxy: the in-flight path, end to end.

The unit tests prove the pending registry and the Correlator separately. This proves the thing
neither can: that mitmproxy's ``request`` hook really does fire before a response exists, so an
Exchange the app is still waiting on shows up in a Timeline as ``pending``.

That mechanism is the whole reason P4 built a pending registry, and it is the hardest half of the
acceptance criterion — "the Exchange still in flight when the assertion began" for a request that
never finishes. It is proved **here** rather than in the live simulator test, because that one
turned out unable to drive it: `openLink` to a private-IP URL did not make Safari fetch it, so the
sink was never reached and the criterion was met by incidental Safari traffic instead. A test whose
deterministic mechanism silently does not fire is worse than one that admits what it covers.

Needs no simulator and touches no system state: a real in-process proxy, a real `curl`, and a sink
that accepts the connection and never answers.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import threading
import time

import pytest

from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from tool_calls import call

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl"),
]

#: How long to wait for the request to reach the proxy and be registered as open.
PENDING_TIMEOUT_S = 15.0


class HangingSink:
    """Accepts TCP connections and never answers them.

    Held open deliberately: dropping the socket would close it and turn "no response" into
    "connection reset", which mitmproxy records as a completed-with-error Exchange rather than
    leaving it in flight — a different thing entirely, and not what this test is about.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._held: list[socket.socket] = []
        self._running = True
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    def _accept_forever(self) -> None:
        while self._running:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self._held.append(connection)

    def __enter__(self) -> HangingSink:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._running = False
        self._listener.close()
        for connection in self._held:
            connection.close()


async def wait_for_pending(state, deadline: float):
    """Poll until the proxy registers an open request, or give up.

    Polled rather than awaited on an event: the proxy shares this event loop (ADR-0001), so
    yielding is what lets its hooks run at all.
    """
    while time.monotonic() < deadline:
        if state.store.pending:
            return state.store.pending
        await asyncio.sleep(0.05)
    return ()


async def test_a_request_that_never_answers_appears_in_a_timeline_as_pending(tmp_path):
    """The criterion's hardest half, deterministically: the assertion began while a request that
    never came back was outstanding."""
    state = CaptureState(tmp_path)
    server = build_server(state=state)

    with HangingSink() as sink:
        started = await call(server, "start_capture", target="curl", port=0)
        port = started["port"]

        # Fired and left running. `--noproxy ""` is required, or an ambient NO_PROXY sends the
        # request direct and the proxy records nothing at all.
        curl = subprocess.Popen(
            [
                "curl",
                "-s",
                "-x",
                f"http://127.0.0.1:{port}",
                "--noproxy",
                "",
                "--max-time",
                "30",
                f"http://127.0.0.1:{sink.port}/hang",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            pending = await wait_for_pending(state, time.monotonic() + PENDING_TIMEOUT_S)
            assert pending, (
                "the proxy never registered an open request, so the request hook is not firing"
                " before the response — the whole in-flight path depends on it"
            )
            assert "/hang" in pending[0].path

            # No Exchange yet: ids are assigned on completion, and nothing has completed.
            assert len(state.store) == 0

            # A Step that is running while the request is outstanding.
            await call(server, "start_step", label="assertVisible: Home")
            await call(server, "end_step", status="failed", error="never appeared")

            timeline = await call(server, "get_timeline")
        finally:
            curl.kill()
            curl.wait(timeout=10)
            if state.recording:
                await call(server, "stop_capture")

    (open_request,) = timeline["pending"]
    assert open_request["pending"] is True
    # No id, because none has been assigned — the whole point of the parallel registry.
    assert open_request["id"] is None
    assert "/hang" in open_request["path"]

    assertion = timeline["steps"][0]
    assert assertion["status"] == "failed"
    assert [item["id"] for item in assertion["overlapping"]] == [None]
    assert "still in flight" in assertion["explanation"]


async def test_a_pending_request_does_not_satisfy_wait_for_exchanges(tmp_path):
    """The load-bearing consequence of keeping pending out of the store.

    `wait_for_exchanges` exists so an agent can say "tap Login, wait for POST /auth, then check it
    returned 401". If an open request satisfied it, the wait would return before a status code
    existed and that whole pattern would break.
    """
    state = CaptureState(tmp_path)
    server = build_server(state=state)

    with HangingSink() as sink:
        started = await call(server, "start_capture", target="curl", port=0)
        curl = subprocess.Popen(
            [
                "curl",
                "-s",
                "-x",
                f"http://127.0.0.1:{started['port']}",
                "--noproxy",
                "",
                "--max-time",
                "30",
                f"http://127.0.0.1:{sink.port}/hang",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            assert await wait_for_pending(state, time.monotonic() + PENDING_TIMEOUT_S)

            waited = await call(server, "wait_for_exchanges", filter="~u /hang", timeout_ms=1000)
        finally:
            curl.kill()
            curl.wait(timeout=10)
            if state.recording:
                await call(server, "stop_capture")

    assert waited["timed_out"] is True
    assert waited["returned"] == 0
