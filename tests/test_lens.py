"""The Lens: the fourth narrowing, and the two places it deliberately does not reach.

Everything here is about *where* a Lens applies, because that is the whole of the decision. It is
set once on the Session and inherited by every listing through ``state.reporting``, which is why
there is no test that ``list_exchanges`` and ``search_exchanges`` narrow separately — they cannot
diverge. The tests that earn their place are the ones covering paths that assemble their own
result or opt out of the other narrowings: ``get_timeline``, ``wait_for_exchanges`` and
``get_exchange``.

The proxy is stubbed and traffic is put into the store directly. What is under test is the wiring.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from https_proxy_mcp.capture.exchange import CapturedExchange, Exchange
from https_proxy_mcp.capture.reporting import ReportingFilter
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from stub_runtime import StubRuntime
from tool_calls import call

#: The two simulators this whole feature exists for: same app, same host, one machine-wide proxy.
SIDEKICK = '~hq "X-Sim: sidekick"'


def captured(sim: str | None = None, **overrides) -> CapturedExchange:
    """A CapturedExchange, optionally stamped by the build that made it."""
    host = overrides.pop("host", "api.example.com")
    path = overrides.pop("path", "/me")
    headers = [("X-Sim", sim)] if sim is not None else []
    # Now, not a fixed epoch: a Timeline with no run_id is the whole Session, whose window opens
    # when the Session did. Constant timestamps fall outside it and every row vanishes — which is
    # indistinguishable from a Lens that hid them, and is how this test first passed for the
    # wrong reason.
    started = time.time()
    fields = {
        "t_start": started,
        "t_end": started + 0.25,
        "method": "GET",
        "url": f"http://{host}{path}",
        "host": host,
        "path": path,
        "scheme": "http",
        "http_version": "HTTP/1.1",
        "status_code": 200,
        "request_headers": headers,
        "response_headers": [("Content-Type", "application/json")],
        "request_body": b"",
        "response_body": b'{"id":1}',
        "server_address": "93.184.216.34:80",
        "error": None,
    }
    return CapturedExchange(**(fields | overrides))


@pytest.fixture
def state(tmp_path):
    return CaptureState(tmp_path, runtime_factory=StubRuntime)


@pytest.fixture
def server(state):
    return build_server(state=state)


@pytest.fixture
async def lensed(server, state):
    """Recording through a Lens that admits the sidekick simulator and nothing else."""
    await call(server, "start_capture", target="curl", port=0, lens=SIDEKICK)
    yield server
    if state.recording:
        await call(server, "stop_capture")


def both_simulators(state) -> None:
    """One Exchange from each simulator, indistinguishable but for the header."""
    state.store.record(captured("sidekick", path="/sidekick"))
    state.store.record(captured("main", path="/main"))


# --- the expression itself ----------------------------------------------------


def test_a_header_value_with_a_space_must_be_quoted_or_it_matches_nothing(state, server):
    """The trap in ``capture/CLAUDE.md``, pinned.

    Unquoted, the expression *parses* — so ``start_capture`` accepts it — and then admits neither
    the traffic it names nor the traffic it does not. A Lens that silently hides an entire Session
    looks exactly like a capture that recorded nothing, which is the one thing this tool must
    never make an agent believe. Both halves are asserted: the quoted form separates them, the
    unquoted form separates nothing.
    """
    quoted = ReportingFilter(lens=_compile(state, '~hq "X-Sim: sidekick"'))
    unquoted = ReportingFilter(lens=_compile(state, "~hq X-Sim: sidekick"))

    sidekick, main = _exchange("sidekick"), _exchange("main")

    assert (quoted.allows(sidekick), quoted.allows(main)) == (True, False)
    assert (unquoted.allows(sidekick), unquoted.allows(main)) == (False, False)


def _compile(state, expression):
    from https_proxy_mcp.capture.filtering import exchange_matcher

    return exchange_matcher(expression, lambda _exchange, _side: b"")


def _exchange(sim: str) -> Exchange:
    return Exchange(
        id=1,
        t_start=1000.0,
        t_end=1000.25,
        method="GET",
        url="http://api.example.com/me",
        host="api.example.com",
        path="/me",
        scheme="http",
        http_version="HTTP/1.1",
        status_code=200,
        request_headers=[("X-Sim", sim)],
    )


# --- where it applies ---------------------------------------------------------


async def test_a_lens_narrows_a_listing(lensed, state):
    both_simulators(state)

    result = await call(lensed, "list_exchanges")

    assert [item["path"] for item in result["exchanges"]] == ["/sidekick"]
    assert result["hidden"] == 1


async def test_a_lens_narrows_a_timeline(lensed, state):
    """``get_timeline`` assembles its own result, so it is the one that could silently miss this.

    This is the failure the whole feature is for: without it the other simulator's request lands
    inside this Run's window and is reported as traffic this Run's Steps caused.
    """
    both_simulators(state)

    timeline = await call(lensed, "get_timeline", verbosity="full", include_background=True)

    rendered = json.dumps(timeline)
    assert "/sidekick" in rendered
    assert "/main" not in rendered


async def test_a_lens_narrows_a_wait_even_though_the_scope_does_not(lensed, state):
    """The exemption reversal, with its own control.

    ``wait_for_exchanges`` is exempt from the denylist and the Scope because naming a host in a
    filter is explicit. A Lens names a *client*, and a wait that ignored it is satisfied by the
    other simulator's login — the same defect the "only traffic recorded after this call" rule
    exists to prevent, arriving by a different route.

    The control matters more than the assertion: without it this test would pass on a Lens that
    was never applied at all, because nothing had been recorded either.
    """
    assert await _waits_for(lensed, state, sim="sidekick") is False
    assert await _waits_for(lensed, state, sim="main") is True


async def _waits_for(server, state, *, sim: str) -> bool:
    """Whether a wait for ``/late`` times out while that simulator makes exactly that request."""

    async def record_shortly() -> None:
        await asyncio.sleep(0.02)
        state.store.record(captured(sim, path="/late"))

    recorder = asyncio.create_task(record_shortly())
    result = await call(server, "wait_for_exchanges", filter="~u /late", timeout_ms=400)
    await recorder
    return bool(result["timed_out"])


async def test_a_timed_out_wait_names_the_lens_that_could_explain_it(lensed, state):
    """ "It did happen and the Lens excluded it" is otherwise indistinguishable from "it never
    happened", which is the entire answer this tool exists to give."""
    result = await call(lensed, "wait_for_exchanges", filter="~u /nothing", timeout_ms=50)

    assert result["timed_out"] is True
    assert result["lens"] == SIDEKICK


# --- where it deliberately does not -------------------------------------------


async def test_get_exchange_is_exempt_from_the_lens(lensed, state):
    """An id came from somewhere. Consistent with the denylist and Scope exemption."""
    both_simulators(state)
    excluded = [item for item in state.store if item.path == "/main"]

    fetched = await call(lensed, "get_exchange", exchange_id=excluded[0].id)

    assert fetched["path"] == "/main"


def test_a_lens_cannot_narrow_a_host_question():
    """A TLS handshake failure and a pending request are not Exchanges, so there is nothing to
    test an expression against — and the ``tls_failed`` count stays machine-wide, exactly as
    ``diagnose_capture`` says it is."""
    refuse_everything = ReportingFilter(lens=lambda _exchange: False)

    assert refuse_everything.allows_host("api.example.com") is True


async def test_the_tls_failure_count_is_unmoved_by_a_lens(server, state):
    await call(
        server,
        "start_capture",
        target="curl",
        port=0,
        scope=["api.example.com"],
        lens='~hq "X-Sim: nobody"',
    )
    state.store.record_tls_failure("api.example.com", "handshake failed")

    assert state.observations().tls_failures == 1

    await call(server, "stop_capture")


# --- refusal ------------------------------------------------------------------


async def test_a_bad_lens_is_refused_before_anything_is_arranged(server, state, tmp_path):
    """Compiled first, so a typo cannot be discovered after the machine's proxy has been
    redirected and an authority installed into a device. Proven by the absence of a Session
    directory: creating one is the first thing ``start`` does after preparing the target."""
    with pytest.raises(Exception, match="invalid filter expression"):
        await call(server, "start_capture", target="curl", port=0, lens="~hq (unclosed")

    assert state.recording is False
    assert list((tmp_path / "sessions").glob("*")) == []


# --- reported -----------------------------------------------------------------


async def test_the_lens_is_reported_where_it_was_set_and_while_it_holds(server, state):
    started = await call(server, "start_capture", target="curl", port=0, lens=SIDEKICK)
    status = await call(server, "get_status")

    assert started["lens"] == SIDEKICK
    assert status["lens"] == SIDEKICK

    await call(server, "stop_capture")


async def test_no_lens_reports_null_rather_than_an_empty_expression(server, state):
    """An empty string is a Lens that matches everything, and null is no Lens at all. Reporting
    the first for the second would tell an agent it had asked for something it had not."""
    started = await call(server, "start_capture", target="curl", port=0)

    assert started["lens"] is None

    await call(server, "stop_capture")
