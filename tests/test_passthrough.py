"""Passthrough: a Scope narrows what is *decrypted*, and everything else is forwarded untouched.

The headline behaviour. A Session redirects the whole machine's proxy, so before this every HTTPS
connection on the Mac was served a leaf signed by the Interception Authority — while that authority
is installed into exactly one Capture Target. The developer's browser and every other simulator
broke, for traffic nobody asked to capture.

What is asserted here is the **certificate the client saw**, because that is the only thing that
separates *this connection was not opened* from *it was opened and merely not recorded*. Asserting
that no Exchange was recorded passes equally in both cases — and passes again if the connection was
dropped altogether, which is why the passed-through body is asserted in the same breath.

One TLS fixture stands as both hosts. `localhost` and `127.0.0.1` reach the same listener, and
mitmproxy's candidate strings are `host:port`, so the two names are distinguishable to the engine
while the bytes behind them are identical — a difference in outcome can only come from the decision
under test.

The `integration` mark is per test rather than per module on purpose: the first two sections bind a
real port and move real bytes, while the last drives `CaptureState` through the non-binding runtime
stub and belongs in the fast subset.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from mitmproxy.options import Options

from fixture_server import GET_RESPONSE, TlsFixture, serve_tls
from https_proxy_mcp.capture import ExchangeStore
from https_proxy_mcp.capture.hostsets import allow_host_patterns
from https_proxy_mcp.capture.runtime import ProxyRuntime
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from stub_runtime import StubRuntime

# One definition of the name the two halves are read apart by, and of how a common name is dug out
# of `ssl`'s nested RDN tuples. Both belong to the file that establishes the fixture's own identity;
# restating either here would let this test and that one disagree about what they are comparing.
from test_fixture_server import INTERCEPTION_AUTHORITY_NAME, common_name
from tool_calls import call

CAPTURE_TIMEOUT_S = 15.0

#: mitmproxy writes its authority here, under the confdir the runtime is given. What a client has to
#: trust to reach an *intercepted* host, and nothing else has any reason to trust.
AUTHORITY_FILENAME = "mitmproxy-ca-cert.pem"


@dataclass(frozen=True)
class Seen:
    """What one client learned by making one request: who signed the certificate, and the body."""

    issuer: str
    body: bytes


# --- driving real traffic ------------------------------------------------------


def get_through_proxy(proxy_port: int, host: str, fixture: TlsFixture, cafile: Path) -> Seen:
    """A proxied TLS GET to ``host``, reporting the issuer the client saw and the body it got.

    Trust is exactly ``cafile``, so the issuer is not merely *reported*: a certificate signed by
    anything else fails the handshake rather than being read back and asserted about.

    ``set_tunnel`` is what makes this a proxied request — CONNECT first, then TLS to ``host`` — so
    the name in the CONNECT authority is the one the engine decides on. The peer certificate is read
    straight after `connect()`, the habit `CLAUDE.md` records: `http.client` drops its socket when a
    response says close, and the question then raises `AttributeError` instead of answering.
    """
    context = ssl.create_default_context(cafile=str(cafile))
    connection = http.client.HTTPSConnection("127.0.0.1", proxy_port, context=context, timeout=10)
    connection.set_tunnel(host, fixture.port)
    try:
        connection.connect()
        assert connection.sock is not None
        issuer = common_name(connection.sock.getpeercert()["issuer"])
        connection.request("GET", "/me")
        return Seen(issuer=issuer, body=connection.getresponse().read())
    finally:
        connection.close()


async def get_through(runtime: ProxyRuntime, host: str, fixture: TlsFixture, cafile: Path) -> Seen:
    """The same request, off the event loop the proxy is running on.

    A blocking request from an async test starves the proxy that is meant to answer it — ADR-0001
    puts both on one loop — and it reads as a hang rather than as a mistake.
    """
    return await asyncio.to_thread(get_through_proxy, runtime.port, host, fixture, cafile)


def options_of(runtime: ProxyRuntime) -> Options:
    """The live mitmproxy options of a started runtime.

    Reaching past `ProxyRuntime`'s surface, deliberately and in one place. `allow_hosts` is
    mitmproxy's option and the runtime exposes no way to read or set it, which is right: the
    alternative is a product parameter that exists only so a test can pass it.
    """
    return runtime._master.options


def trust_the_fixture_upstream(runtime: ProxyRuntime, authority: Path) -> None:
    """Tell the engine to trust the fixture's authority on the connection it opens *upstream*.

    Only the intercepted half needs this: mitmproxy verifies the server it connects to, and the
    fixture's authority is unknown to the system, so without it an intercepted request answers
    `502 Certificate verify failed` with the real body nowhere in sight (`CLAUDE.md`). The issuer
    the client saw is the same either way; the body is not, and a 502 would leave the intercepted
    half proving less than the passed-through one.
    """
    options_of(runtime).update(ssl_verify_upstream_trusted_ca=str(authority))


@asynccontextmanager
async def started(
    store: ExchangeStore,
    confdir: Path,
    *,
    intercept_hosts: tuple[str, ...] | None = None,
    upstream_ca: Path | None = None,
) -> AsyncIterator[ProxyRuntime]:
    """A runtime on an ephemeral port for the duration of the block, always stopped afterwards."""
    runtime = ProxyRuntime(store, port=0, confdir=confdir, intercept_hosts=intercept_hosts)
    await runtime.start()
    try:
        if upstream_ca is not None:
            trust_the_fixture_upstream(runtime, upstream_ca)
        yield runtime
    finally:
        await runtime.stop()


async def until_recorded(store: ExchangeStore, count: int) -> None:
    """Wait for the addon to have recorded ``count`` Exchanges.

    The response hook is not guaranteed to have run by the time the client holds its bytes, so a
    test waits for the store rather than assuming.
    """
    async with asyncio.timeout(CAPTURE_TIMEOUT_S):
        while store.recorded_count < count:
            await asyncio.sleep(0.02)


@pytest.fixture
def store(tmp_path) -> Iterator[ExchangeStore]:
    with ExchangeStore(tmp_path / "session") as store:
        yield store


@pytest.fixture(scope="module")
def confdir(tmp_path_factory) -> Path:
    """One confdir for the module, so the Interception Authority is generated once rather than per
    test. Under pytest's temporary tree, never the developer's `~/.mitmproxy`."""
    return tmp_path_factory.mktemp("confdir")


@pytest.fixture(scope="module")
def tls(tmp_path_factory) -> Iterator[TlsFixture]:
    """The TLS fixture, with an identity of its own — `test_fixture_server.py` establishes it."""
    with serve_tls(tmp_path_factory.mktemp("tls")) as fixture:
        yield fixture


# --- what the engine is told ---------------------------------------------------


@pytest.mark.integration
async def test_a_scoped_runtime_tells_the_engine_which_hosts_to_open(store, confdir):
    """Rendered by `hostsets`, never written here. Two expressions of one host-set rule is the trap
    `capture/CLAUDE.md` records, and at this layer it would surface as a Scope that reports a host's
    traffic while never decrypting it."""
    hosts = ("kivid.com.br", "*.example.com")

    async with started(store, confdir, intercept_hosts=hosts) as runtime:
        allowed = options_of(runtime).allow_hosts

    # Non-empty first: an equality between two empty lists would prove nothing at all.
    assert allowed
    assert allowed == list(allow_host_patterns(hosts))


@pytest.mark.integration
async def test_an_unscoped_runtime_leaves_the_engine_intercepting_everything(store, confdir):
    """Today's behaviour, and the default: with nothing to distinguish, everything is opened."""
    async with started(store, confdir) as runtime:
        assert options_of(runtime).allow_hosts == []


# --- the certificate the client saw --------------------------------------------


@pytest.mark.integration
async def test_the_certificate_says_which_connection_was_opened(store, confdir, tls):
    """The whole claim Passthrough makes, and the only assertion that can carry it.

    The passed-through host answers with the fixture's own issuer — nothing opened the connection,
    so nothing could re-sign it — while the host in the Scope answers with the Interception
    Authority's. The body comes back both ways: without it a connection that was quietly dropped
    would read exactly like one that was forwarded, and with it the two halves differ in the
    certificate alone, which is the difference under test.
    """
    authority = confdir / AUTHORITY_FILENAME

    async with started(
        store, confdir, intercept_hosts=("localhost",), upstream_ca=tls.authority_pem
    ) as runtime:
        assert authority.exists(), f"no Interception Authority at {authority}"
        passed_through = await get_through(runtime, "127.0.0.1", tls, tls.authority_pem)
        intercepted = await get_through(runtime, "localhost", tls, authority)

    assert passed_through.issuer == tls.issuer
    assert intercepted.issuer == INTERCEPTION_AUTHORITY_NAME
    assert (passed_through.body, intercepted.body) == (GET_RESPONSE, GET_RESPONSE)


@pytest.mark.integration
async def test_only_the_host_in_scope_becomes_an_exchange(store, confdir, tls):
    """The other half: a connection nobody opened cannot be recorded, and one that was is.

    Ordered so the passed-through request has every chance to be recorded before the count is read —
    it goes first, and the wait is for the *intercepted* one that follows it.
    """
    async with started(
        store, confdir, intercept_hosts=("localhost",), upstream_ca=tls.authority_pem
    ) as runtime:
        await get_through(runtime, "127.0.0.1", tls, tls.authority_pem)
        await get_through(runtime, "localhost", tls, confdir / AUTHORITY_FILENAME)
        await until_recorded(store, 1)

        assert [exchange.host for exchange in store] == ["localhost"]
        # Nor is it left in flight: a passed-through request never reaches the request hook either.
        assert store.pending == ()


# --- the plumbing, and what it reports -----------------------------------------


@pytest.fixture
def runtimes() -> list[StubRuntime]:
    return []


@pytest.fixture
async def state(tmp_path, runtimes):
    """A state whose runtimes are handed over for inspection rather than bound to a port.

    What the Session decided to decrypt is only visible in what it hands the runtime, so the factory
    records them instead of the test reaching into the state afterwards.
    """

    def factory(store, **options):
        runtime = StubRuntime(store, **options)
        runtimes.append(runtime)
        return runtime

    state = CaptureState(tmp_path / "root", runtime_factory=factory)
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
def server(state):
    return build_server(state=state)


async def test_a_scope_is_the_set_of_hosts_the_runtime_is_told_to_open(server, runtimes):
    """Passthrough takes the Scope's hosts as its input rather than a second list: an agent keeping
    two nearly-identical host lists in step is a trap, and in practice they are always the same."""
    await call(server, "start_capture", port=0, scope=["api.example.com"])

    assert runtimes[-1].intercept_hosts == ("api.example.com",)


async def test_without_a_scope_nothing_is_passed_through(server, runtimes):
    """Inert by construction, which is what makes the default safe to have on: with no Scope there
    is nothing to distinguish, so exploratory capture still shows everything on the machine."""
    await call(server, "start_capture", port=0)

    assert runtimes[-1].intercept_hosts is None


async def test_passthrough_can_be_switched_off_despite_a_scope(server, runtimes):
    """The escape hatch for a host the agent did not think to name."""
    await call(server, "start_capture", port=0, scope=["api.example.com"], passthrough=False)

    assert runtimes[-1].intercept_hosts is None


async def test_an_empty_scope_is_not_a_set_of_hosts_to_open(server, runtimes):
    """An empty Scope reports nothing (`reporting.py`), and naming no hosts to decrypt would mean
    recording nothing at all. Everything is intercepted instead, so the traffic is still reachable
    by id or in an export."""
    await call(server, "start_capture", port=0, scope=[])

    assert runtimes[-1].intercept_hosts is None


async def test_the_start_result_says_passthrough_is_in_force(server):
    """The default engaging is never silent: this is the sentence an agent relays to a developer."""
    result = await call(server, "start_capture", port=0, scope=["api.example.com"])

    assert result["passthrough"] is True


async def test_the_start_result_says_when_passthrough_is_inert(server):
    """Reported as the condition, not as the parameter: `passthrough=True` with no Scope decrypts
    everything, and a result claiming otherwise would be a promise about the developer's browser
    that this Session is not keeping."""
    result = await call(server, "start_capture", port=0)

    assert result["passthrough"] is False


async def test_the_start_result_says_when_passthrough_was_switched_off(server):
    result = await call(
        server, "start_capture", port=0, scope=["api.example.com"], passthrough=False
    )

    assert result["passthrough"] is False


async def test_the_manifest_records_passthrough_beside_the_scope(server, state):
    """So a Session directory found later answers "why is there no traffic from that host?"."""
    await call(server, "start_capture", port=0, scope=["api.example.com"])

    manifest = json.loads((state.session.dir / "session.json").read_text())
    assert (manifest["scope"], manifest["passthrough"]) == (["api.example.com"], True)
