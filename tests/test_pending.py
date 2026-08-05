"""In-flight requests: the store's pending registry, and the addon hook that fills it.

The store records on *completion*, which is right for everything P1-P3 does and useless for the
one question P4 exists to answer: was the request the failing assertion depended on still
outstanding when it began? A request that never comes back is not in the store at all.

The whole design constraint here is that this must not disturb Exchanges. Ids are assigned on
completion so cursor tailing means "what finished since I last looked", and `wait_for_exchanges`
depends on a recorded Exchange having a status code. So pending requests live in a **parallel**
registry that no listing tool reads.
"""

from __future__ import annotations

import pytest
from mitmproxy.test import tflow

from https_proxy_mcp.capture import ExchangeStore
from https_proxy_mcp.capture.addon import CaptureAddon
from https_proxy_mcp.capture.exchange import CapturedExchange, PendingExchange
from https_proxy_mcp.capture.store import PENDING_CAP


@pytest.fixture
def store(tmp_path):
    with ExchangeStore(tmp_path / "session") as store:
        yield store


@pytest.fixture
def addon(store):
    return CaptureAddon(store)


def pending(key: str, t_start: float = 1.0, path: str = "/") -> PendingExchange:
    return PendingExchange(
        key=key,
        t_start=t_start,
        method="GET",
        url=f"https://api.example.com{path}",
        host="api.example.com",
        path=path,
        scheme="https",
    )


def captured(t_start: float = 1.0, t_end: float | None = 2.0) -> CapturedExchange:
    return CapturedExchange(
        t_start=t_start,
        t_end=t_end,
        method="GET",
        url="https://api.example.com/",
        host="api.example.com",
        path="/",
        scheme="https",
        http_version="HTTP/1.1",
        status_code=200,
    )


# --- the registry -------------------------------------------------------------


def test_an_opened_request_is_pending(store):
    store.open(pending("a"))

    assert [item.key for item in store.pending] == ["a"]


def test_completing_a_request_clears_it_from_pending(store):
    store.open(pending("a"))

    store.record(captured(), pending_key="a")

    assert store.pending == ()


def test_completing_without_a_key_leaves_pending_alone(store):
    """Every existing caller of `record` passes no key, and none of them should be able to
    silently empty the registry."""
    store.open(pending("a"))

    store.record(captured())

    assert [item.key for item in store.pending] == ["a"]


def test_an_unknown_key_on_completion_is_not_an_error(store):
    """mitmproxy delivers a response hook for a flow whose request hook this store never saw —
    anything already in flight when capture started. Raising would turn a normal race into an
    addon error in the proxy's log."""
    store.record(captured(), pending_key="never-seen")

    assert store.pending == ()


def test_pending_requests_are_ordered_oldest_first(store):
    store.open(pending("a", t_start=1.0))
    store.open(pending("b", t_start=2.0))
    store.open(pending("c", t_start=3.0))

    assert [item.key for item in store.pending] == ["a", "b", "c"]


def test_reopening_the_same_key_does_not_duplicate_it(store):
    store.open(pending("a"))
    store.open(pending("a"))

    assert len(store.pending) == 1


# --- Exchanges are untouched --------------------------------------------------


def test_opening_a_request_records_no_exchange_and_consumes_no_id(store):
    """The load-bearing assertion of this whole unit. If opening a request took an id, the
    documented completion ordering would be inverted and `wait_for_exchanges` would fire before
    a status code existed."""
    store.open(pending("a"))
    store.open(pending("b"))

    assert len(store) == 0
    assert store.recorded_count == 0

    recorded = store.record(captured(), pending_key="a")
    # The first completion is still id 1, not id 3.
    assert recorded.id == 1


def test_a_pending_request_is_invisible_to_iteration(store):
    """Every listing tool iterates the store, so this is what keeps list_exchanges,
    tail_exchanges, search_exchanges and wait_for_exchanges unchanged."""
    store.open(pending("a"))

    assert list(store) == []


# --- bounds -------------------------------------------------------------------


def test_pending_requests_are_capped_and_the_overflow_is_counted(store):
    """A server that accepts and never answers, hit in a loop, must not grow without bound —
    the same reasoning as the TLS-failure cap."""
    for index in range(PENDING_CAP + 5):
        store.open(pending(f"key-{index}", t_start=float(index)))

    assert len(store.pending) == PENDING_CAP
    assert store.pending_dropped == 5
    # The oldest went, so the newest are the ones retained.
    assert store.pending[-1].key == f"key-{PENDING_CAP + 4}"


# --- lifecycle ----------------------------------------------------------------


def test_pending_requests_survive_close(store):
    """"Two requests were still open when the Session ended" is information, not garbage. It is
    also the honest answer to "why did the assertion time out?"."""
    store.open(pending("a"))

    store.close()

    assert [item.key for item in store.pending] == ["a"]


def test_opening_a_request_after_close_is_counted_not_recorded(store):
    """Same shape as `record` after close: connections still in flight when stop_capture runs
    deliver their hooks afterwards, and an exception there reads as a defect in the proxy log."""
    store.close()

    store.open(pending("a"))

    assert store.pending == ()
    assert store.dropped_after_close == 1


# --- the addon hook ----------------------------------------------------------


def test_the_request_hook_opens_a_pending_record(addon, store):
    flow = tflow.tflow()
    flow.request.method = "POST"
    flow.request.path = "/graphql"

    addon.request(flow)

    (opened,) = store.pending
    assert opened.key == flow.id
    assert (opened.method, opened.path) == ("POST", "/graphql")
    assert opened.host == flow.request.pretty_host
    assert opened.t_start == flow.request.timestamp_start


def test_a_response_clears_the_pending_record_it_opened(addon, store):
    flow = tflow.tflow(resp=True)

    addon.request(flow)
    addon.response(flow)

    assert store.pending == ()
    assert len(store) == 1


def test_an_error_clears_the_pending_record_too(addon, store):
    """A request that failed is no longer in flight — it is a recorded Exchange with no end.
    Leaving it pending would double-count it in every Timeline."""
    flow = tflow.tflow(err=True)

    addon.request(flow)
    addon.error(flow)

    assert store.pending == ()
    assert len(store) == 1


def test_a_request_with_no_response_stays_pending(addon, store):
    """The acceptance criterion's shape: read in full, never answered."""
    addon.request(tflow.tflow())

    assert len(store.pending) == 1
    assert len(store) == 0
