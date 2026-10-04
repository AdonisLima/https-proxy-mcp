"""A local server for tests that need real traffic to flow, over plain HTTP or over TLS.

Shared rather than redefined per test module, so there is one description of what the
fixture answers. Deliberately not named ``test_*`` so pytest does not collect it.

Two modes, chosen by which context manager the caller enters. `serve` is the plain-HTTP one every
existing caller uses. `serve_tls` answers the same shapes behind TLS, and exists because some
questions can only be answered by the certificate a client saw: *not intercepted* and *intercepted
and merely not recorded* are indistinguishable from the store, and the issuer is what separates
them. So the TLS mode has an identity of its own — its own certificate authority, deliberately not
the Interception Authority — established by `test_fixture_server.py` rather than assumed by the
tests that read it.

The identity is minted per call from ``mitmproxy.certs``, which is already a dependency of the
project under test. Nothing is checked in, and everything written lands in the directory the caller
names, which is a test's ``tmp_path``.
"""

from __future__ import annotations

import ipaddress
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy import certs

#: What ``GET`` on any ordinary path returns.
GET_RESPONSE = b'{"id":1,"name":"fixture"}'

#: What ``POST`` on any path returns.
POST_RESPONSE = b'{"created":true}'

#: ``GET /big`` returns this, to exercise preview truncation and body slicing.
BIG_RESPONSE = b"".join(f"line {n:04d}\n".encode() for n in range(1, 1001))

#: ``GET /secret`` returns a body a redacting serializer must not pass through.
SECRET_VALUE = "sk-live-do-not-leak"
SECRET_RESPONSE = f'{{"access_token":"{SECRET_VALUE}","expires_in":3600}}'.encode()

#: The basename of the TLS mode's own authority, and so the name its issuer carries. Chosen to be
#: nothing like ``mitmproxy``: a test proving a connection was passed through reads the two apart.
AUTHORITY_BASENAME = "fixture-server"

#: The two loopback names the TLS leaf covers. mitmproxy matches its candidates as ``host:port``
#: strings, so these are distinguishable to it — which is what lets one fixture stand as an
#: intercepted host and a passed-through host in the same test.
TLS_NAMES = ("localhost", "127.0.0.1")


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


@dataclass(frozen=True)
class TlsFixture:
    """A running TLS fixture: where to reach it, what to trust it with, and who signed its leaf."""

    #: The ephemeral port it listens on.
    port: int

    #: Its own authority, certificate only, in PEM. What a client passes as ``cafile``.
    authority_pem: Path

    #: The common name the leaf names as its issuer, read off the authority the fixture minted
    #: rather than restated here, so what the fixture reports is what it is actually serving.
    issuer: str


@contextmanager
def _running(server: ThreadingHTTPServer) -> Iterator[int]:
    """Serve on a daemon thread for the duration of the block, then release the port.

    `shutdown` stops the accept loop and `server_close` closes the listener. Measured: either one
    alone still releases the port, because the socket's last reference goes with this frame — but
    neither does not, so the pair stays as an explicit protocol rather than a reliance on when
    CPython happens to collect a socket.

    The poll interval is shortened from the stdlib's half a second because `shutdown` blocks until
    the accept loop next looks: the plain mode is entered once for the whole test session and never
    noticed, while a TLS fixture is entered per test and would pay it every time.
    """
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def serve() -> Iterator[int]:
    """Run the fixture server on an ephemeral port for the duration of the block."""
    with _running(ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)) as port:
        yield port


@contextmanager
def serve_tls(directory: Path) -> Iterator[TlsFixture]:
    """Run the fixture server over TLS, under an identity minted into ``directory``.

    ``directory`` is created if it does not exist and is the only place anything is written, so it
    belongs inside the calling test's ``tmp_path`` — it holds a private key.
    """
    authority_pem, leaf_pem, issuer = _mint_identity(directory)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf_pem)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    # Wrapping the listening socket puts the handshake on the accept loop, which for a fixture
    # answering one client at a time is simpler than deferring it into each handler thread.
    server.socket = context.wrap_socket(server.socket, server_side=True)

    with _running(server) as port:
        yield TlsFixture(port=port, authority_pem=authority_pem, issuer=issuer)


def _mint_identity(directory: Path) -> tuple[Path, Path, str]:
    """Create an authority and a leaf for the loopback names. Returns authority, leaf, issuer.

    Uses `mitmproxy.certs`, already a dependency, so this needs no new one and no key material in
    the repository. The leaf carries a DNS name *and* an IP address, because a client verifying
    ``127.0.0.1`` checks the address against the certificate the same way it checks a hostname.
    """
    directory.mkdir(parents=True, exist_ok=True)
    store = certs.CertStore.from_store(directory, AUTHORITY_BASENAME, key_size=2048)
    entry = store.get_cert(TLS_NAMES[0], [_subject_alt_name(name) for name in TLS_NAMES])

    leaf_pem = directory / "leaf.pem"
    leaf_pem.write_bytes(
        entry.cert.to_pem()
        + entry.privatekey.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    issuer = store.default_ca.cn
    assert issuer is not None, "the minted authority has no common name to be recognised by"
    return directory / f"{AUTHORITY_BASENAME}-ca-cert.pem", leaf_pem, issuer


def _subject_alt_name(name: str) -> x509.GeneralName:
    """``name`` as a subject alternative name, as an address if it is one and a hostname if not."""
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)
