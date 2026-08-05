"""Unit tests for the capture addon: mitmproxy's flow objects in, Exchanges out.

Driven with mitmproxy's own ``tflow`` fixtures rather than a live proxy, so these stay fast
and cover the shapes a live proxy makes awkward to produce on demand — a compressed body, a
malformed encoding, a connection that errored before any response.

The end-to-end criterion (real ``mitmdump``, real ``curl``) lives in test_capture_e2e.py.
"""

import gzip

import pytest
from mitmproxy.http import Headers
from mitmproxy.test import tflow

from https_proxy_mcp.capture import ExchangeStore
from https_proxy_mcp.capture.addon import CaptureAddon


@pytest.fixture
def store(tmp_path):
    with ExchangeStore(tmp_path / "session") as store:
        yield store


@pytest.fixture
def addon(store):
    return CaptureAddon(store)


# --- the response hook --------------------------------------------------------


def test_a_completed_response_is_recorded(addon, store):
    addon.response(tflow.tflow(resp=True))

    assert len(store) == 1


def test_request_metadata_is_taken_from_the_flow(addon, store):
    flow = tflow.tflow(resp=True)
    flow.request.method = "POST"
    flow.request.path = "/auth"

    addon.response(flow)

    recorded = store.get(1)
    assert (recorded.method, recorded.path) == ("POST", "/auth")
    assert recorded.host == flow.request.pretty_host
    assert recorded.url == flow.request.url
    assert recorded.scheme == flow.request.scheme
    assert recorded.http_version == flow.request.http_version


def test_the_response_status_is_recorded(addon, store):
    flow = tflow.tflow(resp=True)
    flow.response.status_code = 401

    addon.response(flow)

    assert store.get(1).status_code == 401


def test_the_interval_spans_request_start_to_response_end(addon, store):
    """Overlap attribution is interval arithmetic, so both edges must be the real ones."""
    flow = tflow.tflow(resp=True)
    flow.request.timestamp_start = 100.0
    flow.response.timestamp_end = 100.9

    addon.response(flow)

    recorded = store.get(1)
    assert (recorded.t_start, recorded.t_end) == (100.0, 100.9)
    assert recorded.duration_ms == pytest.approx(900.0)


def test_the_server_address_is_recorded(addon, store):
    addon.response(tflow.tflow(resp=True))

    assert store.get(1).server_address == "192.168.0.1:22"


def test_a_flow_with_no_server_connection_records_no_address(addon, store):
    flow = tflow.tflow(resp=True)
    flow.server_conn.peername = None

    addon.response(flow)

    assert store.get(1).server_address is None


def test_a_completed_response_records_no_error(addon, store):
    addon.response(tflow.tflow(resp=True))

    assert store.get(1).error is None


# --- headers ------------------------------------------------------------------


def test_request_headers_are_recorded(addon, store):
    flow = tflow.tflow(resp=True)
    flow.request.headers = Headers([(b"authorization", b"Bearer abc")])

    addon.response(flow)

    assert store.get(1).request_headers == [("authorization", "Bearer abc")]


def test_repeated_response_headers_are_all_kept(addon, store):
    """Set-Cookie legitimately repeats; collapsing it to a mapping would lose one."""
    flow = tflow.tflow(resp=True)
    flow.response.headers = Headers([(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")])

    addon.response(flow)

    assert store.get(1).response_headers == [("set-cookie", "a=1"), ("set-cookie", "b=2")]


# --- bodies -------------------------------------------------------------------


def test_bodies_are_passed_through_to_the_store(addon, store):
    flow = tflow.tflow(resp=True)
    flow.request.content = b"grant_type=password"
    flow.response.content = b'{"ok":true}'

    addon.response(flow)

    recorded = store.get(1)
    assert (store.dir / recorded.request_body.path).read_bytes() == b"grant_type=password"
    assert (store.dir / recorded.response_body.path).read_bytes() == b'{"ok":true}'


def test_a_compressed_body_is_stored_decoded(addon, store):
    """Bodies are for reading. A gzipped blob on disk answers no debugging question."""
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "gzip"
    flow.response.raw_content = gzip.compress(b'{"id":1}')

    addon.response(flow)

    recorded = store.get(1)
    assert (store.dir / recorded.response_body.path).read_bytes() == b'{"id":1}'


def test_a_malformed_encoding_is_stored_raw_rather_than_raising(addon, store):
    """A single corrupt body must not take down capture for the rest of the Session."""
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "gzip"
    flow.response.raw_content = b"not-actually-gzip"

    addon.response(flow)

    recorded = store.get(1)
    assert (store.dir / recorded.response_body.path).read_bytes() == b"not-actually-gzip"


def test_an_absent_body_spills_no_file(addon, store):
    flow = tflow.tflow(resp=True)
    flow.request.content = b""

    addon.response(flow)

    assert store.get(1).request_body.path is None


# --- the error hook -----------------------------------------------------------


def test_an_errored_flow_is_recorded_rather_than_dropped(addon, store):
    """A request that never got a response is often exactly the diagnostic fact."""
    addon.error(tflow.tflow(err=True))

    assert len(store) == 1


def test_an_errored_flow_records_the_reason(addon, store):
    flow = tflow.tflow(err=True)
    flow.error.msg = "connection refused"

    addon.error(flow)

    assert store.get(1).error == "connection refused"


def test_an_errored_flow_has_no_status_and_no_end(addon, store):
    addon.error(tflow.tflow(err=True))

    recorded = store.get(1)
    assert recorded.status_code is None
    assert recorded.t_end is None
    assert recorded.duration_ms is None


def test_an_errored_flow_still_records_its_request(addon, store):
    flow = tflow.tflow(err=True)
    flow.request.path = "/timeout"

    addon.error(flow)

    assert store.get(1).path == "/timeout"


def test_an_errored_flow_records_no_response_headers(addon, store):
    addon.error(tflow.tflow(err=True))

    assert store.get(1).response_headers == []


# --- ordering -----------------------------------------------------------------


def test_exchanges_are_numbered_in_completion_order(addon, store):
    for path in ("/one", "/two"):
        flow = tflow.tflow(resp=True)
        flow.request.path = path
        addon.response(flow)

    assert [(e.id, e.path) for e in store] == [(1, "/one"), (2, "/two")]


# --- TLS handshake failures ---------------------------------------------------
#
# These never become Exchanges — the handshake dies before there is a request — so without
# this hook a pinned app and a silent app are indistinguishable, which is exactly the
# confusion diagnose_capture exists to remove.


def test_a_failed_client_handshake_is_counted(addon, store):
    addon.tls_failed_client(_tls_failure())

    assert store.tls_failure_count == 1


def test_a_failed_handshake_records_the_host_that_failed(addon, store):
    """Which host failed is the whole signal: one host failing while others succeed is
    pinning, everything failing is an untrusted authority."""
    addon.tls_failed_client(_tls_failure(sni="api.example.com"))

    assert store.tls_failures[0].host == "api.example.com"


def test_a_failed_handshake_records_the_reason(addon, store):
    addon.tls_failed_client(_tls_failure(error="Client and server cipher mismatch"))

    assert "cipher mismatch" in store.tls_failures[0].reason


def test_a_handshake_failure_with_no_sni_is_still_recorded(addon, store):
    """An app connecting by IP sends no SNI. Dropping those would hide the failure."""
    addon.tls_failed_client(_tls_failure(sni=None))

    assert store.tls_failure_count == 1
    assert store.tls_failures[0].host is None


def test_a_failed_handshake_is_not_an_exchange(addon, store):
    """No request ever arrived, so recording one would invent traffic that never happened."""
    addon.tls_failed_client(_tls_failure())

    assert len(store) == 0
    assert store.recorded_count == 0


def _tls_failure(*, sni: str | None = "example.com", error: str = "unknown error"):
    """A stand-in for mitmproxy's TlsData on the client-failure path.

    tflow has no helper for this hook, and the addon reads only two fields off the
    connection, so a real TlsData would add setup without adding coverage.
    """

    class _Connection:
        def __init__(self) -> None:
            self.sni = sni
            self.error = error

    class _TlsData:
        def __init__(self) -> None:
            self.conn = _Connection()

    return _TlsData()
