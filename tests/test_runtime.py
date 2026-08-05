"""Tests for the in-process proxy runtime.

ADR-0001 puts the MCP server and the mitmproxy addon in one process sharing one
ExchangeStore. These tests hold that arrangement to account: the proxy runs as a task on the
server's own event loop, records into the store the server reads from, binds only to
loopback, and — the part that is easy to get wrong — actually releases its port when stopped.

Marked `integration` because they bind real sockets and move real bytes.
"""

from __future__ import annotations

import asyncio
import socket
import urllib.request
from collections.abc import AsyncIterator, Iterator

import pytest

from https_proxy_mcp.capture import ExchangeStore
from https_proxy_mcp.capture.runtime import ProxyRuntime

pytestmark = pytest.mark.integration

CAPTURE_TIMEOUT_S = 15.0


def accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def fetch(proxy_port: int, url: str) -> bytes:
    """A blocking proxied GET, to be handed to a thread."""
    handler = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"})
    return urllib.request.build_opener(handler).open(url, timeout=10).read()


async def fetch_through(runtime: ProxyRuntime, url: str) -> bytes:
    return await asyncio.to_thread(fetch, runtime.port, url)


async def until_recorded(store: ExchangeStore, count: int) -> None:
    """Wait for the addon to have recorded ``count`` Exchanges.

    The response hook is not guaranteed to have run by the time the client has its bytes,
    so tests wait for the store rather than assuming.
    """
    async with asyncio.timeout(CAPTURE_TIMEOUT_S):
        while store.recorded_count < count:
            await asyncio.sleep(0.02)


@pytest.fixture
def store(tmp_path) -> Iterator[ExchangeStore]:
    with ExchangeStore(tmp_path / "session") as store:
        yield store


@pytest.fixture
async def running(store, tmp_path) -> AsyncIterator[ProxyRuntime]:
    """A started runtime on an ephemeral port, always stopped afterwards."""
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy")
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


# --- starting -----------------------------------------------------------------


async def test_the_proxy_listens_once_started(running):
    assert accepts(running.port)


async def test_an_ephemeral_port_is_reported_back(running):
    """Asking for port 0 and being told what you got is what makes tests hermetic."""
    assert running.port != 0


async def test_a_requested_port_is_honoured(store, tmp_path, free_port):
    runtime = ProxyRuntime(store, port=free_port, confdir=tmp_path / "mitmproxy")
    await runtime.start()
    try:
        assert runtime.port == free_port
    finally:
        await runtime.stop()


async def test_the_proxy_binds_only_to_loopback(running):
    """An open proxy reachable from the LAN would be a hole, not a feature."""
    assert running.host == "127.0.0.1"


async def test_starting_twice_is_refused(running):
    with pytest.raises(RuntimeError, match="already"):
        await running.start()


# --- capturing ----------------------------------------------------------------


async def test_traffic_through_the_proxy_reaches_the_store(running, store, fixture_server):
    await fetch_through(running, f"http://127.0.0.1:{fixture_server}/me")
    await until_recorded(store, 1)

    assert [exchange.path for exchange in store] == ["/me"]


async def test_the_captured_exchange_carries_real_metadata(running, store, fixture_server):
    await fetch_through(running, f"http://127.0.0.1:{fixture_server}/me")
    await until_recorded(store, 1)

    exchange = store.get(1)
    assert exchange.method == "GET"
    assert exchange.status_code == 200
    assert exchange.host == "127.0.0.1"


async def test_bodies_are_spilled_by_the_running_proxy(running, store, fixture_server):
    await fetch_through(running, f"http://127.0.0.1:{fixture_server}/me")
    await until_recorded(store, 1)

    assert store.read_body(store.get(1), "response") == b'{"id":1,"name":"fixture"}'


# --- stopping -----------------------------------------------------------------


async def test_stopping_releases_the_port(store, tmp_path):
    """Clearing mitmproxy's mode is what closes the listener; shutdown alone does not."""
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy")
    await runtime.start()
    port = runtime.port

    await runtime.stop()

    assert not accepts(port)


async def test_the_same_port_can_be_reused_after_stopping(store, tmp_path, free_port):
    first = ProxyRuntime(store, port=free_port, confdir=tmp_path / "mitmproxy")
    await first.start()
    await first.stop()

    second = ProxyRuntime(store, port=free_port, confdir=tmp_path / "mitmproxy")
    await second.start()
    try:
        assert accepts(free_port)
    finally:
        await second.stop()


async def test_stopping_twice_is_harmless(store, tmp_path):
    """stop_capture must not explode because teardown already happened."""
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy")
    await runtime.start()

    await runtime.stop()
    await runtime.stop()


async def test_a_runtime_that_never_started_can_be_stopped(store, tmp_path):
    await ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy").stop()


async def test_is_running_tracks_the_lifecycle(store, tmp_path):
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy")
    assert runtime.is_running is False

    await runtime.start()
    assert runtime.is_running is True

    await runtime.stop()
    assert runtime.is_running is False
