"""A local HTTP server for tests that need real traffic to flow.

Shared rather than redefined per test module, so there is one description of what the
fixture answers. Deliberately not named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: What ``GET`` on any ordinary path returns.
GET_RESPONSE = b'{"id":1,"name":"fixture"}'

#: What ``POST`` on any path returns.
POST_RESPONSE = b'{"created":true}'

#: ``GET /big`` returns this, to exercise preview truncation and body slicing.
BIG_RESPONSE = b"".join(f"line {n:04d}\n".encode() for n in range(1, 1001))

#: ``GET /secret`` returns a body a redacting serializer must not pass through.
SECRET_VALUE = "sk-live-do-not-leak"
SECRET_RESPONSE = f'{{"access_token":"{SECRET_VALUE}","expires_in":3600}}'.encode()


class FixtureHandler(BaseHTTPRequestHandler):
    """Answers the handful of shapes the tests need, and stays quiet in pytest output."""

    protocol_version = "HTTP/1.1"

    # The do_VERB names are dispatched on by BaseHTTPRequestHandler and cannot be renamed.

    def do_GET(self) -> None:
        if self.path.startswith("/big"):
            self._respond(200, BIG_RESPONSE)
        elif self.path.startswith("/secret"):
            self._respond(200, SECRET_RESPONSE)
        else:
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


@contextmanager
def serve() -> Iterator[int]:
    """Run the fixture server on an ephemeral port for the duration of the block."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
