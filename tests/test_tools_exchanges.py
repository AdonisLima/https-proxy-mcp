"""Tests for the read-only Exchange tools, driven through FastMCP's own dispatch.

Calls go through ``call_tool``, so argument schemas and error translation are exercised
rather than bypassed. The proxy is stubbed out — the addon-to-store path has its own tests,
and what matters here is the wiring: which parameters narrow what, what the envelopes report,
and how the failures read to an agent that has to recover from them.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture import CapturedExchange
from https_proxy_mcp.serialize import digest
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from stub_runtime import StubRuntime
from tool_calls import call

TOKEN = "Bearer s3cret-token"


def captured(**overrides) -> CapturedExchange:
    """A CapturedExchange whose url agrees with its host and path, as a real one always does."""
    host = overrides.pop("host", "api.example.com")
    path = overrides.pop("path", "/me")
    fields = {
        "t_start": 1000.0,
        "t_end": 1000.25,
        "method": "GET",
        "url": f"http://{host}{path}",
        "host": host,
        "path": path,
        "scheme": "http",
        "http_version": "HTTP/1.1",
        "status_code": 200,
        "request_headers": [("Authorization", TOKEN)],
        "response_headers": [("Content-Type", "application/json")],
        "request_body": b"",
        "response_body": b'{"id":1}',
        "server_address": "93.184.216.34:80",
        "error": None,
    }
    return CapturedExchange(**(fields | overrides))


@pytest.fixture
async def state(tmp_path):
    state = CaptureState(tmp_path / "root", runtime_factory=StubRuntime)
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
async def recording(state):
    """A state that is recording, with the proxy stubbed."""
    await state.start(target="curl", port=0, scope=None)
    return state


@pytest.fixture
def server(state):
    return build_server(state=state)


# --- nothing recording --------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_exchanges", {}),
        ("get_exchange", {"exchange_id": 1}),
        ("get_exchange_body", {"exchange_id": 1, "side": "response"}),
        ("search_exchanges", {"filter": "~c 200"}),
        ("tail_exchanges", {}),
        ("wait_for_exchanges", {"filter": "~c 200", "timeout_ms": 100}),
        ("export_curl", {"exchange_id": 1}),
    ],
)
async def test_a_read_tool_says_what_to_do_when_nothing_is_recording(server, tool, arguments):
    with pytest.raises(ToolError, match="start_capture"):
        await server.call_tool(tool, arguments)


# --- listing ------------------------------------------------------------------


async def test_listing_returns_summaries(server, recording):
    recording.store.record(captured(path="/one"))
    recording.store.record(captured(path="/two"))

    result = await call(server, "list_exchanges")

    assert [row["path"] for row in result["exchanges"]] == ["/one", "/two"]
    assert result["returned"] == 2


async def test_a_listing_never_contains_a_body(server, recording):
    recording.store.record(captured(response_body=b'{"very":"secret"}'))

    result = await call(server, "list_exchanges")

    assert "secret" not in json.dumps(result)


async def test_a_listing_reports_evictions(server, tmp_path):
    """ "Nothing older" and "something older, now gone" are different answers."""
    state = CaptureState(tmp_path / "root", runtime_factory=StubRuntime)
    await state.start(target="curl", port=0, scope=None, capacity=2)
    try:
        for _ in range(5):
            state.store.record(captured())

        result = await call(build_server(state=state), "list_exchanges")

        assert result["evicted"] == 3
        assert result["recorded"] == 5
    finally:
        await state.stop()


async def test_a_listing_pages_with_a_cursor(server, recording):
    for index in range(5):
        recording.store.record(captured(path=f"/{index}"))

    first = await call(server, "list_exchanges", limit=2)
    second = await call(server, "list_exchanges", limit=2, cursor=first["next_cursor"])

    assert [row["id"] for row in first["exchanges"]] == [1, 2]
    assert [row["id"] for row in second["exchanges"]] == [3, 4]


async def test_the_final_page_has_no_cursor(server, recording):
    recording.store.record(captured())

    assert (await call(server, "list_exchanges"))["next_cursor"] is None


@pytest.mark.parametrize(
    ("narrowing", "expected"),
    [
        ({"host": "api.example.com"}, ["/me", "/submit", "/broken"]),
        ({"host": "other.example.com"}, []),
        ({"method": "POST"}, ["/submit"]),
        ({"status": 500}, ["/broken"]),
        ({"since": 2000.0}, ["/submit", "/broken"]),
        ({"until": 1500.0}, ["/me"]),
    ],
)
async def test_a_listing_can_be_narrowed(server, recording, narrowing, expected):
    recording.store.record(captured(path="/me", t_start=1000.0))
    recording.store.record(captured(path="/submit", method="POST", t_start=3000.0))
    recording.store.record(captured(path="/broken", status_code=500, t_start=3000.0))

    result = await call(server, "list_exchanges", **narrowing)

    assert [row["path"] for row in result["exchanges"]] == expected


async def test_the_host_narrowing_ignores_case(server, recording):
    recording.store.record(captured(host="API.Example.COM"))

    result = await call(server, "list_exchanges", host="api.example.com")

    assert result["returned"] == 1


async def test_an_oversized_page_is_clamped_and_says_so(server, recording):
    recording.store.record(captured())

    result = await call(server, "list_exchanges", limit=10_000)

    assert result["limit"] == 200


# --- one Exchange -------------------------------------------------------------


async def test_getting_an_exchange_returns_detail(server, recording):
    recording.store.record(captured())

    result = await call(server, "get_exchange", exchange_id=1)

    assert result["url"] == "http://api.example.com/me"
    assert result["response_body"]["preview"] == '{"id":1}'


async def test_getting_an_exchange_redacts_by_default(server, recording):
    recording.store.record(captured())

    result = await call(server, "get_exchange", exchange_id=1)

    assert result["request_headers"] == [["Authorization", digest(TOKEN)]]


async def test_getting_an_exchange_can_include_secrets(server, recording):
    recording.store.record(captured())

    result = await call(server, "get_exchange", exchange_id=1, include_secrets=True)

    assert result["request_headers"] == [["Authorization", TOKEN]]


async def test_an_unknown_exchange_is_reported_as_unknown(server, recording):
    recording.store.record(captured())

    with pytest.raises(ToolError, match="99"):
        await server.call_tool("get_exchange", {"exchange_id": 99})


async def test_an_evicted_exchange_is_reported_as_evicted(server, tmp_path):
    """An agent that knows it was evicted stops retrying and looks at the log instead."""
    state = CaptureState(tmp_path / "root", runtime_factory=StubRuntime)
    await state.start(target="curl", port=0, scope=None, capacity=2)
    try:
        for _ in range(5):
            state.store.record(captured())

        with pytest.raises(ToolError, match="evicted"):
            await build_server(state=state).call_tool("get_exchange", {"exchange_id": 1})
    finally:
        await state.stop()


# --- body slices --------------------------------------------------------------


async def test_a_body_slice_returns_a_window(server, recording):
    recording.store.record(captured(response_body=b"0123456789"))

    result = await call(
        server, "get_exchange_body", exchange_id=1, side="response", offset=2, length=4
    )

    assert result["data"] == "2345"
    assert result["total"] == 10
    assert result["has_more"] is True


async def test_a_body_slice_can_read_the_request_side(server, recording):
    recording.store.record(captured(request_body=b"grant_type=client_credentials"))

    result = await call(server, "get_exchange_body", exchange_id=1, side="request")

    assert "client_credentials" in result["data"]


async def test_an_unknown_side_is_rejected(server, recording):
    recording.store.record(captured())

    with pytest.raises(ToolError, match="side"):
        await server.call_tool("get_exchange_body", {"exchange_id": 1, "side": "sideways"})


async def test_a_body_slice_redacts_by_default(server, recording):
    recording.store.record(captured(response_body=b'{"access_token":"s3cret"}'))

    result = await call(server, "get_exchange_body", exchange_id=1, side="response")

    assert "s3cret" not in result["data"]


# --- search -------------------------------------------------------------------


async def test_search_uses_mitmproxy_filter_expressions(server, recording):
    recording.store.record(captured(path="/me", status_code=200))
    recording.store.record(captured(path="/broken", status_code=500))

    result = await call(server, "search_exchanges", filter="~c 500")

    assert [row["path"] for row in result["exchanges"]] == ["/broken"]


async def test_search_can_match_a_body(server, recording):
    recording.store.record(captured(response_body=b'{"error":"needle"}'))
    recording.store.record(captured(response_body=b'{"ok":true}'))

    result = await call(server, "search_exchanges", filter="~bs needle")

    assert result["returned"] == 1


async def test_an_invalid_filter_is_reported_as_invalid(server, recording):
    with pytest.raises(ToolError, match="filter"):
        await server.call_tool("search_exchanges", {"filter": "~~~bogus((("})


async def test_search_pages_like_listing_does(server, recording):
    for index in range(5):
        recording.store.record(captured(path=f"/{index}"))

    first = await call(server, "search_exchanges", filter="~c 200", limit=2)

    assert first["next_cursor"] == "2"


# --- tail ---------------------------------------------------------------------


async def test_tailing_from_no_cursor_returns_what_is_there(server, recording):
    recording.store.record(captured(path="/one"))

    result = await call(server, "tail_exchanges")

    assert [row["path"] for row in result["exchanges"]] == ["/one"]
    assert result["next_cursor"] == "1"


async def test_an_oversized_tail_is_clamped_like_every_other_listing(server, recording):
    """The one listing that did not go through `paginate`, and so did not inherit its ceiling.

    SPEC.md's claim is that flooding the context is *impossible* rather than discouraged, and a
    tail that honoured `limit=10_000` was a hole in exactly that — reachable by asking for the
    other browse tool, which is the same shape as the denylist bypass `tail_exchanges` already
    guards against. Found while writing the usage guide's reference table, because documenting the
    200-row cap meant checking that it applied everywhere it was claimed.
    """
    for index in range(205):
        recording.store.record(captured(path=f"/{index}"))

    result = await call(server, "tail_exchanges", limit=10_000)

    assert result["returned"] == 200
    assert result["limit"] == 200
    assert result["more_available"] is True


async def test_tailing_returns_only_what_is_new(server, recording):
    recording.store.record(captured(path="/one"))
    first = await call(server, "tail_exchanges")
    recording.store.record(captured(path="/two"))

    second = await call(server, "tail_exchanges", cursor=first["next_cursor"])

    assert [row["path"] for row in second["exchanges"]] == ["/two"]


async def test_tailing_with_nothing_new_keeps_the_cursor(server, recording):
    """Unlike a listing, a tail's cursor stays usable — more traffic is expected later."""
    recording.store.record(captured())
    first = await call(server, "tail_exchanges")

    second = await call(server, "tail_exchanges", cursor=first["next_cursor"])

    assert second["exchanges"] == []
    assert second["next_cursor"] == first["next_cursor"]


async def test_tailing_can_be_filtered(server, recording):
    recording.store.record(captured(path="/me", status_code=200))
    recording.store.record(captured(path="/broken", status_code=500))

    result = await call(server, "tail_exchanges", filter="~c 500")

    assert [row["path"] for row in result["exchanges"]] == ["/broken"]


# --- waiting ------------------------------------------------------------------


async def test_waiting_returns_when_a_matching_exchange_arrives(server, recording):
    async def later():
        await asyncio.sleep(0.05)
        recording.store.record(captured(path="/late"))

    task = asyncio.create_task(later())
    try:
        result = await call(server, "wait_for_exchanges", filter="~u /late", timeout_ms=5000)
    finally:
        await task

    assert result["timed_out"] is False
    assert [row["path"] for row in result["exchanges"]] == ["/late"]


async def test_waiting_ignores_traffic_that_arrived_before_the_call(server, recording):
    """ "Tap Login, then wait for POST /auth" must not be satisfied by a previous /auth."""
    recording.store.record(captured(path="/early"))

    result = await call(server, "wait_for_exchanges", filter="~u /early", timeout_ms=150)

    assert result["timed_out"] is True
    assert result["exchanges"] == []


async def test_waiting_times_out_without_raising(server, recording):
    """A timeout is an answer — "it never happened" — not an error."""
    result = await call(server, "wait_for_exchanges", filter="~u /never", timeout_ms=150)

    assert result["timed_out"] is True


async def test_waiting_can_require_several_matches(server, recording):
    async def later():
        for _ in range(3):
            await asyncio.sleep(0.02)
            recording.store.record(captured(path="/poll"))

    task = asyncio.create_task(later())
    try:
        result = await call(
            server, "wait_for_exchanges", filter="~u /poll", min_count=3, timeout_ms=5000
        )
    finally:
        await task

    assert result["returned"] == 3


async def test_an_excessive_wait_is_clamped(server, recording):
    """Satisfied immediately, because waiting out the clamped two minutes proves nothing."""

    async def later():
        await asyncio.sleep(0.05)
        recording.store.record(captured(path="/soon"))

    task = asyncio.create_task(later())
    try:
        result = await call(server, "wait_for_exchanges", filter="~u /soon", timeout_ms=10_000_000)
    finally:
        await task

    assert result["timeout_ms"] == 120_000


# --- curl export --------------------------------------------------------------


async def test_exporting_curl_reproduces_the_request(server, recording):
    recording.store.record(captured())

    result = await call(server, "export_curl", exchange_id=1)

    assert result["command"].startswith("curl -X GET http://api.example.com/me")


async def test_exporting_curl_redacts_by_default(server, recording):
    recording.store.record(captured())

    result = await call(server, "export_curl", exchange_id=1)

    assert "s3cret" not in result["command"]


async def test_exporting_curl_can_be_made_runnable(server, recording):
    recording.store.record(captured())

    result = await call(server, "export_curl", exchange_id=1, include_secrets=True)

    assert TOKEN in result["command"]


# --- reporting is narrowed by the denylist and the Scope -----------------------
#
# Capture stays unconditional; these tests are about what an agent is shown. The counts are
# asserted as hard as the rows are: quietly dropping traffic would read as "there was none".


async def test_os_chatter_is_left_out_of_a_listing(server, recording):
    recording.store.record(captured(host="api.example.com"))
    recording.store.record(captured(host="gs-loc.apple.com"))

    result = await call(server, "list_exchanges")

    assert [row["host"] for row in result["exchanges"]] == ["api.example.com"]


async def test_a_listing_says_how_much_it_hid(server, recording):
    """Silence here is the failure mode: an agent shown two rows out of four, with no note,
    concludes it has seen everything."""
    recording.store.record(captured(host="api.example.com"))
    recording.store.record(captured(host="ocsp.digicert.com"))
    recording.store.record(captured(host="configuration.apple.com"))

    result = await call(server, "list_exchanges")

    assert result["hidden"] == 2


async def test_chatter_can_be_asked_for_explicitly(server, recording):
    recording.store.record(captured(host="gs-loc.apple.com"))

    result = await call(server, "list_exchanges", include_denied=True)

    assert result["returned"] == 1
    assert result["hidden"] == 0


async def test_a_session_scope_narrows_a_listing(state):
    """The Scope was already recorded in session.json and echoed by get_status; until now no
    read tool consulted it (HANDOFF.md, "deliberately not built yet")."""
    await state.start(target="curl", port=0, scope=["api.example.com"])
    server = build_server(state=state)
    state.store.record(captured(host="api.example.com"))
    state.store.record(captured(host="cdn.example.com"))

    result = await call(server, "list_exchanges")

    assert [row["host"] for row in result["exchanges"]] == ["api.example.com"]
    assert result["hidden"] == 1


async def test_a_scope_beats_the_denylist(state):
    """Naming a denied host explicitly is an instruction, not a mistake to correct."""
    await state.start(target="curl", port=0, scope=["configuration.apple.com"])
    server = build_server(state=state)
    state.store.record(captured(host="configuration.apple.com"))

    result = await call(server, "list_exchanges")

    assert result["returned"] == 1


async def test_a_search_is_narrowed_too(server, recording):
    """Otherwise the denylist is trivially defeated by using the other listing tool."""
    recording.store.record(captured(host="api.example.com"))
    recording.store.record(captured(host="gs-loc.apple.com"))

    result = await call(server, "search_exchanges", filter="~c 200")

    assert [row["host"] for row in result["exchanges"]] == ["api.example.com"]


async def test_a_tail_is_narrowed_too(server, recording):
    recording.store.record(captured(host="api.example.com"))
    recording.store.record(captured(host="ocsp.digicert.com"))

    result = await call(server, "tail_exchanges")

    assert [row["host"] for row in result["exchanges"]] == ["api.example.com"]


async def test_waiting_is_not_narrowed(server, recording):
    """Deliberately exempt. wait_for_exchanges is handed an explicit filter naming what to wait
    for, so letting the denylist veto it would turn "wait for this request" into a timeout that
    reports the request never happened — a lie, and a very expensive one to debug.
    """
    async def record_soon():
        await asyncio.sleep(0.05)
        recording.store.record(captured(host="configuration.apple.com"))

    task = asyncio.create_task(record_soon())
    result = await call(server, "wait_for_exchanges", filter="~d configuration.apple.com",
                        timeout_ms=3000)
    await task

    assert result["timed_out"] is False
    assert result["returned"] == 1


async def test_fetching_one_exchange_by_id_is_not_narrowed(server, recording):
    """An id came from somewhere, so refusing it would be obstruction rather than filtering —
    and `hidden` in the listing is what tells an agent the traffic exists to ask about."""
    recording.store.record(captured(host="gs-loc.apple.com"))

    result = await call(server, "get_exchange", exchange_id=1)

    assert result["host"] == "gs-loc.apple.com"
