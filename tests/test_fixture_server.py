"""The shared fixture server's TLS mode, and the identity it serves.

Most of this suite drives the fixture over plain HTTP, and that mode is unchanged. This file is
about the second one: the same handler behind TLS, with a leaf minted per test run from
``mitmproxy.certs`` — already a dependency, so no new one and no key material checked in.

The mode exists for an assertion that cannot be made any other way. *This connection was not
intercepted* and *it was intercepted and merely not recorded* look identical from the store, and
the certificate the client saw is the only thing that separates them. That makes the fixture's
**own** identity load-bearing, so it is established here rather than assumed at the point of use: a
real client fetches over TLS and reads the issuer back off the socket, and that issuer is asserted
to be something other than the name the Interception Authority carries — which is the difference
the later assertion reads.

One fixture answers to both ``localhost`` and ``127.0.0.1``. mitmproxy's candidate strings
distinguish the two, so one server can stand as an intercepted host and a passed-through host in the
same test without a second listener.

The plain mode's callers are unmodified — `test_runtime.py`, `test_capture_e2e.py` and
`test_tools_e2e.py` — so their staying green is what says the TLS mode changed nothing for them.

Marked `integration` because it binds a real socket and moves real bytes.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from pathlib import Path

import pytest

from fixture_server import GET_RESPONSE, TlsFixture, serve, serve_tls

pytestmark = pytest.mark.integration

#: The name mitmproxy gives its own authority. What the fixture's issuer must *not* be, or the
#: intercepted case and the passed-through case would be indistinguishable at the point of use.
INTERCEPTION_AUTHORITY_NAME = "mitmproxy"


def common_name(rdns: tuple[tuple[tuple[str, str], ...], ...]) -> str:
    """The ``commonName`` out of the nested RDN tuples `ssl` hands back for a subject or issuer."""
    for rdn in rdns:
        for attribute, value in rdn:
            if attribute == "commonName":
                return str(value)
    raise AssertionError(f"no commonName in {rdns!r}")


def fetch_over_tls(host: str, fixture: TlsFixture, path: str = "/me") -> tuple[bytes, str]:
    """A direct TLS GET. Returns the body, and the issuer common name the client saw on the wire.

    Trust is the fixture's own authority and nothing else, so a certificate signed by anything
    else — mitmproxy's authority included — fails the handshake rather than being read back.

    The certificate is read straight after `connect()`, which is the habit `../tests/CLAUDE.md`
    records: `http.client` drops its socket whenever a response says close, and then the question
    raises `AttributeError` and reads like a failed request.
    """
    context = ssl.create_default_context(cafile=str(fixture.authority_pem))
    connection = http.client.HTTPSConnection(host, fixture.port, context=context, timeout=10)
    try:
        connection.connect()
        assert connection.sock is not None
        issuer = common_name(connection.sock.getpeercert()["issuer"])
        connection.request("GET", path)
        return connection.getresponse().read(), issuer
    finally:
        connection.close()


def accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


# --- the identity, which is what the mode is for ------------------------------


def test_the_client_reads_back_the_fixtures_own_issuer(tmp_path):
    """The whole reason for the mode: the fixture's identity is measured, not assumed."""
    with serve_tls(tmp_path / "tls") as fixture:
        body, issuer = fetch_over_tls("localhost", fixture)

    assert body == GET_RESPONSE
    assert issuer == fixture.issuer


def test_the_fixtures_issuer_is_not_the_interception_authority(tmp_path):
    """Without this the later assertion is unfalsifiable.

    If the fixture minted its leaf from mitmproxy's own confdir, "the client saw the fixture's
    issuer" and "the client saw the Interception Authority" would read the same string, and a test
    claiming a connection was passed through would pass while it was being decrypted.
    """
    with serve_tls(tmp_path / "tls") as fixture:
        _, issuer = fetch_over_tls("localhost", fixture)

    assert issuer != INTERCEPTION_AUTHORITY_NAME


def test_one_fixture_answers_to_both_loopback_names(tmp_path):
    """`localhost` and `127.0.0.1` are one server and two names, which is what lets a single fixture
    be the intercepted host and the passed-through host in the same test. Both have to verify
    against the fixture's authority, so the leaf carries a name and an address."""
    with serve_tls(tmp_path / "tls") as fixture:
        by_name = fetch_over_tls("localhost", fixture)
        by_address = fetch_over_tls("127.0.0.1", fixture)

    assert by_name == by_address == (GET_RESPONSE, fixture.issuer)


def test_each_fixture_mints_its_own_authority(tmp_path):
    """No key material is checked in, and none is shared: two fixtures are two identities.

    A checked-in or cached authority would read as working right up to the point where two of these
    run at once, and byte-identical authority material is what would say it had happened.
    """
    with serve_tls(tmp_path / "first") as first, serve_tls(tmp_path / "second") as second:
        assert first.authority_pem.read_bytes() != second.authority_pem.read_bytes()
        assert first.port != second.port


# --- where it writes ----------------------------------------------------------


def test_nothing_is_written_outside_the_directory_it_was_given(tmp_path):
    """Those directories hold key material, so the fixture writes where the test says and nowhere
    else. Asserted over the whole temporary tree rather than by naming the files it does write,
    because the interesting failure is a file nobody thought to name."""
    directory = tmp_path / "tls"

    with serve_tls(directory) as fixture:
        pass

    written = {path for path in tmp_path.rglob("*") if path.is_file()}
    assert written
    assert written == {path for path in directory.rglob("*") if path.is_file()}
    assert fixture.authority_pem.parent == directory


def test_no_authority_is_written_into_the_developers_home(tmp_path):
    """Belt and braces on top of the directory assertion, naming the file the obvious mistake
    creates: minting from mitmproxy's default confdir writes the fixture's authority into a real
    `~/.mitmproxy`, where it would outlive the test run."""
    with serve_tls(tmp_path / "tls") as fixture:
        pass

    assert not (Path.home() / ".mitmproxy" / fixture.authority_pem.name).exists()


# --- the lifecycle, which the plain mode already gets right --------------------


def test_the_tls_fixture_releases_its_port(tmp_path):
    """The plain mode closes its listener on the way out and so must this one, or a session's worth
    of fixtures leaves a session's worth of listening sockets behind.

    What it proves, established by breaking it: leaving the server running fails this. Dropping
    *either* `shutdown` or `server_close` does not, because the socket's last reference goes with
    the context manager's frame. So read this as "the fixture stops", not as "`server_close` is
    what releases the port".
    """
    with serve_tls(tmp_path / "tls") as fixture:
        assert accepts(fixture.port)

    assert not accepts(fixture.port)


def test_the_plain_mode_is_still_plain():
    """The contract every existing caller depends on: a port, answering `http://`.

    Here because the modes are now a choice, and the choice has a wrong answer — a fixture that
    quietly started requiring TLS would break those callers with a handshake error rather than a
    signature change.
    """
    with serve() as port:
        assert isinstance(port, int)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/me")
            assert connection.getresponse().read() == GET_RESPONSE
        finally:
            connection.close()
