"""Tests for the in-process proxy runtime.

ADR-0001 puts the MCP server and the mitmproxy addon in one process sharing one
ExchangeStore. These tests hold that arrangement to account: the proxy runs as a task on the
server's own event loop, records into the store the server reads from, binds only to
loopback, and — the part that is easy to get wrong — actually releases its port when stopped.

Marked `integration` because they bind real sockets and move real bytes.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Iterator

import pytest
from mitmproxy.tools.web import webaddons

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


def get(url: str) -> tuple[int, bytes]:
    """A blocking plain GET that reports its status instead of raising, to be handed to a thread."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


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


# --- the Human View ------------------------------------------------------------


def split_web_url(web_url: str) -> tuple[str, str]:
    """The view's base URL and its auth token, for building probes against."""
    parsed = urllib.parse.urlparse(web_url)
    return f"{parsed.scheme}://{parsed.netloc}", urllib.parse.parse_qs(parsed.query)["token"][0]


@pytest.fixture
async def web_running(store, tmp_path) -> AsyncIterator[ProxyRuntime]:
    """A started runtime serving the Human View on an ephemeral port, always stopped afterwards."""
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


async def test_the_view_reports_a_token_gated_loopback_url(web_running):
    web_url = web_running.web_url
    assert web_url is not None

    parsed = urllib.parse.urlparse(web_url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port not in (None, 0)
    assert "token=" in parsed.query


async def test_a_requested_web_port_is_honoured(store, tmp_path, free_port):
    runtime = ProxyRuntime(
        store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True, web_port=free_port
    )
    await runtime.start()
    try:
        web_url = runtime.web_url
        assert web_url is not None
        assert urllib.parse.urlparse(web_url).port == free_port
    finally:
        await runtime.stop()


async def test_the_view_serves_the_ui_to_the_token_holder(web_running):
    assert web_running.web_url is not None

    status, body = await asyncio.to_thread(get, web_running.web_url)

    assert status == 200
    assert b'<div id="mitmproxy">' in body


async def test_the_view_refuses_a_request_without_the_token(web_running):
    assert web_running.web_url is not None
    base, _ = split_web_url(web_running.web_url)

    status, _ = await asyncio.to_thread(get, base)

    assert status == 403


async def test_a_request_through_the_proxy_appears_in_the_view(web_running, store, fixture_server):
    """The view and the ExchangeStore are two readings of the one core."""
    await fetch_through(web_running, f"http://127.0.0.1:{fixture_server}/me")
    await until_recorded(store, 1)

    assert web_running.web_url is not None
    base, token = split_web_url(web_running.web_url)
    status, body = await asyncio.to_thread(get, f"{base}/flows.json?token={token}")

    assert status == 200
    entries = json.loads(body)
    assert any(
        entry["request"]["host"] == "127.0.0.1" and entry["request"]["path"] == "/me"
        for entry in entries
    )


async def test_stopping_releases_the_web_port(store, tmp_path):
    """The library never stops its own web server, so the runtime keeps the reference and does."""
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True)
    await runtime.start()
    web_url = runtime.web_url
    assert web_url is not None
    web_port = urllib.parse.urlparse(web_url).port
    assert web_port is not None
    assert accepts(web_port)

    await runtime.stop()

    assert not accepts(web_port)
    assert runtime.web_url is None


async def test_the_browser_is_never_opened(store, tmp_path, monkeypatch):
    """A headless agent server must not pop a browser on the developer's screen."""

    def explode(url: str) -> bool:
        raise AssertionError(f"the Human View tried to open a browser at {url}")

    monkeypatch.setattr(webaddons, "open_browser", explode)

    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True)
    await runtime.start()
    try:
        assert runtime.web_url is not None
    finally:
        await runtime.stop()


async def test_without_the_flag_no_view_is_served(running):
    """Every existing caller gets today's proxy: no view, no URL, nothing extra bound."""
    assert running.web_url is None
    assert accepts(running.port)


async def test_an_occupied_web_port_fails_start_and_cleans_up(store, tmp_path, free_port):
    """Losing the port must fail start with the existing half-started cleanup."""
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        occupied = blocker.getsockname()[1]

        runtime = ProxyRuntime(
            store,
            port=free_port,
            confdir=tmp_path / "mitmproxy",
            web_ui=True,
            web_port=occupied,
        )
        with pytest.raises(OSError):
            await runtime.start()

        assert runtime.is_running is False
        assert not accepts(free_port)
        assert runtime.web_url is None


async def test_stopping_twice_is_harmless_with_the_view_on(store, tmp_path):
    runtime = ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True)
    await runtime.start()

    await runtime.stop()
    await runtime.stop()


async def test_a_web_runtime_that_never_started_can_be_stopped(store, tmp_path):
    await ProxyRuntime(store, port=0, confdir=tmp_path / "mitmproxy", web_ui=True).stop()
