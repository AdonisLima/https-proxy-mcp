"""P1 acceptance: real ``mitmdump`` with the addon loaded, driven by real ``curl``.

This is the criterion from PLAN.md P1, checked as written rather than approximated. The unit
tests cover shapes a live proxy makes awkward to produce; this one covers the thing they
cannot — that the addon actually loads into mitmproxy, that a real proxied request arrives
as a correct Exchange, and that bodies land on disk.

No mobile tooling and no TLS: HTTPS interception needs the Interception Authority trusted
by a Capture Target, which is P3. A local fixture server over plain HTTP exercises
everything P1 owns.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from https_proxy_mcp.capture import Exchange
from https_proxy_mcp.serialize import redact_headers
from mitmdump_loader import SESSION_DIR_ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl"),
]

LOADER = Path(__file__).parent / "mitmdump_loader.py"
TOKEN = "Bearer e2e-secret-token"
POST_BODY = b'{"username":"ada","password":"lovelace"}'
GET_RESPONSE = b'{"id":1,"name":"fixture"}'
POST_RESPONSE = b'{"created":true}'

#: mitmproxy has a CA to generate on first run, so startup is not instant.
PROXY_START_TIMEOUT_S = 30.0
CAPTURE_TIMEOUT_S = 20.0


# --- the fixture server -------------------------------------------------------


class _FixtureHandler(BaseHTTPRequestHandler):
    """Answers the two requests the test makes, and stays quiet in pytest output."""

    protocol_version = "HTTP/1.1"

    # The do_VERB names are dispatched on by BaseHTTPRequestHandler and cannot be renamed.

    def do_GET(self) -> None:
        self._respond(200, GET_RESPONSE)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._respond(201, POST_RESPONSE)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[int]:
    """A local HTTP server on an ephemeral port. Returns the port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


# --- the proxy ----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_listening(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"mitmdump never listened on {port}")


@dataclass
class _Proxy:
    port: int
    session_dir: Path
    log: Path

    @property
    def exchanges_log(self) -> Path:
        return self.session_dir / "exchanges.ndjson"

    def recorded(self) -> list[Exchange]:
        """Every Exchange in the NDJSON log so far."""
        if not self.exchanges_log.exists():
            return []
        return [
            Exchange.from_log_dict(json.loads(line))
            for line in self.exchanges_log.read_text().splitlines()
            if line
        ]

    def wait_for(self, count: int) -> list[Exchange]:
        """Poll the log until ``count`` Exchanges have landed."""
        deadline = time.monotonic() + CAPTURE_TIMEOUT_S
        while time.monotonic() < deadline:
            recorded = self.recorded()
            if len(recorded) >= count:
                return recorded
            time.sleep(0.1)
        raise AssertionError(
            f"expected {count} Exchanges, got {len(self.recorded())}."
            f" mitmdump output:\n{self.log.read_text()}"
        )


@pytest.fixture(scope="module")
def proxy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Proxy]:
    """A real mitmdump with the capture addon loaded, on its own confdir and port."""
    root = tmp_path_factory.mktemp("proxy")
    session_dir = root / "session"
    log = root / "mitmdump.log"
    port = _free_port()

    # A throwaway confdir keeps the user's real ~/.mitmproxy untouched, and a cleaned
    # environment stops an ambient HTTP_PROXY from being chained upstream.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    env[SESSION_DIR_ENV] = str(session_dir)

    with log.open("w") as sink:
        process = subprocess.Popen(
            [
                # Invoked through this interpreter rather than the installed script, so the
                # addon is loaded by the same Python that has the package importable.
                sys.executable,
                "-c",
                "from mitmproxy.tools.main import mitmdump; mitmdump()",
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(port),
                "--set",
                f"confdir={root / 'mitmproxy'}",
                "--quiet",
                "-s",
                str(LOADER),
            ],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            try:
                _wait_until_listening(port, time.monotonic() + PROXY_START_TIMEOUT_S)
            except TimeoutError as exc:
                raise AssertionError(f"{exc}. mitmdump output:\n{log.read_text()}") from exc
            yield _Proxy(port=port, session_dir=session_dir, log=log)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _curl(proxy: _Proxy, *args: str) -> None:
    """Drive one request through the proxy, failing loudly with curl's own message."""
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        # An empty exclusion list, so nothing — localhost included — bypasses the proxy.
        "--noproxy",
        "",
        "-x",
        f"http://127.0.0.1:{proxy.port}",
        *args,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, f"curl failed: {result.stderr}"


# --- the acceptance check -----------------------------------------------------


@pytest.fixture(scope="module")
def captured(proxy: _Proxy, fixture_server: int) -> dict[str, Exchange]:
    """Drive a GET and a POST through the proxy; return the Exchanges keyed by path."""
    base = f"http://127.0.0.1:{fixture_server}"
    _curl(proxy, f"{base}/me", "-H", f"Authorization: {TOKEN}")
    _curl(
        proxy,
        f"{base}/auth",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        POST_BODY.decode(),
    )

    return {exchange.path: exchange for exchange in proxy.wait_for(2)}


def test_both_requests_were_captured(captured):
    assert sorted(captured) == ["/auth", "/me"]


def test_request_metadata_is_correct(captured, fixture_server):
    exchange = captured["/me"]

    assert exchange.method == "GET"
    assert exchange.host == "127.0.0.1"
    assert exchange.scheme == "http"
    assert exchange.http_version == "HTTP/1.1"
    assert exchange.url == f"http://127.0.0.1:{fixture_server}/me"


def test_response_metadata_is_correct(captured):
    assert captured["/me"].status_code == 200
    assert captured["/auth"].status_code == 201


def test_the_interval_is_real_and_ordered(captured):
    exchange = captured["/me"]

    assert exchange.t_end > exchange.t_start
    assert 0 < exchange.duration_ms < 60_000


def test_exchanges_have_monotonic_ids(captured):
    assert sorted(exchange.id for exchange in captured.values()) == [1, 2]


def test_the_connection_is_recorded(captured, fixture_server):
    assert captured["/me"].server_address == f"127.0.0.1:{fixture_server}"


def test_no_error_is_recorded_for_a_successful_exchange(captured):
    assert captured["/me"].error is None


# --- spilled bodies -----------------------------------------------------------


def test_the_request_body_is_spilled_faithfully(captured, proxy):
    body = captured["/auth"].request_body

    assert body.path == f"bodies/{captured['/auth'].id}.req"
    assert (proxy.session_dir / body.path).read_bytes() == POST_BODY
    assert body.size == len(POST_BODY)
    assert body.truncated is False


def test_response_bodies_are_spilled_faithfully(captured, proxy):
    for path, expected in (("/me", GET_RESPONSE), ("/auth", POST_RESPONSE)):
        spilled = proxy.session_dir / captured[path].response_body.path

        assert spilled.read_bytes() == expected


def test_a_request_with_no_body_spills_no_file(captured):
    assert captured["/me"].request_body.path is None


# --- the store stays faithful; serialization is where secrets stop -------------


def test_the_captured_authorization_header_is_faithful_on_disk(captured):
    """Answering "is my app sending the right token?" requires the real value (SPEC.md)."""
    headers = dict(captured["/me"].request_headers)

    assert headers["Authorization"] == TOKEN


def test_no_captured_authorization_value_survives_default_serialization(captured):
    """The other half of the P1 criterion, against real traffic rather than a fixture."""
    serialized = str(redact_headers(captured["/me"].request_headers))

    assert TOKEN not in serialized
    assert "<redacted:sha256:" in serialized
