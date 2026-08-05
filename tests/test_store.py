"""Unit tests for the ExchangeStore: ids, ring-buffer eviction, body spill, NDJSON log.

The store is the durability layer and it is deliberately *not* a redaction boundary — the
Session directory holds faithful bodies so that "is my app sending the right token?" stays
answerable (SPEC.md). The NDJSON assertions below pin that down, because it is the kind of
thing a later well-meaning change would quietly break.
"""

import json

import pytest

from https_proxy_mcp.capture import CapturedExchange, Exchange, ExchangeStore

TOKEN = "Bearer s3cret-token"


def captured(**overrides) -> CapturedExchange:
    """A complete CapturedExchange, so each test overrides only what it is about."""
    fields = {
        "t_start": 1000.0,
        "t_end": 1000.25,
        "method": "GET",
        "url": "http://api.example.com/me",
        "host": "api.example.com",
        "path": "/me",
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
def store(tmp_path):
    with ExchangeStore(tmp_path / "session") as store:
        yield store


# --- identity -----------------------------------------------------------------


def test_ids_start_at_one_and_increase_monotonically(store):
    ids = [store.record(captured()).id for _ in range(3)]

    assert ids == [1, 2, 3]


def test_a_recorded_exchange_is_retrievable_by_id(store):
    recorded = store.record(captured(path="/orders"))

    assert store.get(recorded.id).path == "/orders"


def test_an_unknown_id_returns_none(store):
    assert store.get(999) is None


def test_duration_is_derived_from_the_interval(store):
    assert store.record(captured(t_start=10.0, t_end=10.25)).duration_ms == 250.0


def test_an_exchange_with_no_response_has_no_duration(store):
    """An errored Exchange has a start but no end; duration must not be invented."""
    assert store.record(captured(t_end=None, status_code=None)).duration_ms is None


# --- ring buffer --------------------------------------------------------------


def test_the_buffer_defaults_to_five_thousand():
    assert ExchangeStore.DEFAULT_CAPACITY == 5000


def test_the_buffer_retains_only_up_to_capacity(tmp_path):
    with ExchangeStore(tmp_path / "s", capacity=3) as store:
        for _ in range(5):
            store.record(captured())

        assert len(store) == 3


def test_the_oldest_exchanges_are_the_ones_evicted(tmp_path):
    with ExchangeStore(tmp_path / "s", capacity=3) as store:
        for _ in range(5):
            store.record(captured())

        assert [e.id for e in store] == [3, 4, 5]


def test_an_evicted_exchange_is_no_longer_retrievable(tmp_path):
    with ExchangeStore(tmp_path / "s", capacity=2) as store:
        first = store.record(captured())
        store.record(captured())
        store.record(captured())

        assert store.get(first.id) is None


def test_evictions_are_counted(tmp_path):
    """The count is always reported, so an agent can tell "no more" from "lost"."""
    with ExchangeStore(tmp_path / "s", capacity=2) as store:
        for _ in range(5):
            store.record(captured())

        assert store.evicted_count == 3


def test_nothing_is_evicted_below_capacity(tmp_path):
    with ExchangeStore(tmp_path / "s", capacity=10) as store:
        store.record(captured())

        assert store.evicted_count == 0


def test_the_total_recorded_count_survives_eviction(tmp_path):
    with ExchangeStore(tmp_path / "s", capacity=2) as store:
        for _ in range(5):
            store.record(captured())

        assert store.recorded_count == 5


# --- body spill ---------------------------------------------------------------


def test_a_response_body_is_spilled_faithfully(store):
    recorded = store.record(captured(response_body=b'{"secret":"kept"}'))

    assert (store.dir / recorded.response_body.path).read_bytes() == b'{"secret":"kept"}'


def test_a_request_body_is_spilled_faithfully(store):
    recorded = store.record(captured(request_body=b"grant_type=password"))

    assert (store.dir / recorded.request_body.path).read_bytes() == b"grant_type=password"


def test_the_two_sides_spill_to_distinct_paths(store):
    recorded = store.record(captured(request_body=b"req", response_body=b"resp"))

    assert recorded.request_body.path == "bodies/1.req"
    assert recorded.response_body.path == "bodies/1.resp"


def test_body_paths_are_relative_so_a_session_stays_movable(store):
    recorded = store.record(captured(request_body=b"req"))

    assert not recorded.request_body.path.startswith("/")


def test_an_empty_body_spills_no_file(store):
    recorded = store.record(captured(request_body=b""))

    assert recorded.request_body.path is None
    assert recorded.request_body.size == 0
    assert list((store.dir / "bodies").glob("*.req")) == []


def test_the_body_cap_defaults_to_ten_megabytes():
    assert ExchangeStore.DEFAULT_BODY_CAP_BYTES == 10 * 1024 * 1024


def test_a_body_beyond_the_cap_is_truncated_on_disk(tmp_path):
    with ExchangeStore(tmp_path / "s", body_cap_bytes=8) as store:
        recorded = store.record(captured(response_body=b"0123456789"))

        assert (store.dir / recorded.response_body.path).read_bytes() == b"01234567"


def test_a_truncated_body_is_marked_and_reports_its_true_size(tmp_path):
    with ExchangeStore(tmp_path / "s", body_cap_bytes=8) as store:
        body = store.record(captured(response_body=b"0123456789")).response_body

        assert body.truncated is True
        assert body.size == 10
        assert body.stored_bytes == 8


def test_a_body_within_the_cap_is_not_marked_truncated(tmp_path):
    with ExchangeStore(tmp_path / "s", body_cap_bytes=8) as store:
        assert store.record(captured(response_body=b"12345678")).response_body.truncated is False


# --- reading bodies back ------------------------------------------------------
#
# The store owns the spill layout, so it owns reading it back. Serialization takes bytes
# and does no I/O, which is what keeps the two testable apart.


def test_a_spilled_response_body_is_read_back(store):
    recorded = store.record(captured(response_body=b'{"id":1}'))

    assert store.read_body(recorded, "response") == b'{"id":1}'


def test_a_spilled_request_body_is_read_back(store):
    recorded = store.record(captured(request_body=b"grant_type=password"))

    assert store.read_body(recorded, "request") == b"grant_type=password"


def test_reading_an_absent_body_gives_no_bytes(store):
    recorded = store.record(captured(request_body=b""))

    assert store.read_body(recorded, "request") == b""


def test_reading_a_truncated_body_gives_what_is_on_disk(tmp_path):
    with ExchangeStore(tmp_path / "s", body_cap_bytes=4) as store:
        recorded = store.record(captured(response_body=b"0123456789"))

        assert store.read_body(recorded, "response") == b"0123"


def test_a_body_is_readable_from_an_exchange_rehydrated_from_the_log(store):
    """Reading takes an Exchange, not an id, so an evicted one still resolves."""
    store.record(captured(response_body=b'{"id":1}'))
    rehydrated = Exchange.from_log_dict(json.loads(store.log_path.read_text().splitlines()[0]))

    assert store.read_body(rehydrated, "response") == b'{"id":1}'


def test_an_unknown_side_is_rejected(store):
    recorded = store.record(captured())

    with pytest.raises(ValueError, match="side"):
        store.read_body(recorded, "sideways")


# --- the NDJSON log -----------------------------------------------------------


def test_one_line_is_appended_per_exchange(store):
    for _ in range(3):
        store.record(captured())

    assert len(store.log_path.read_text().splitlines()) == 3


def test_each_line_round_trips_back_to_the_recorded_exchange(store):
    recorded = store.record(captured(request_body=b"req", response_body=b"resp"))

    line = json.loads(store.log_path.read_text().splitlines()[0])

    assert Exchange.from_log_dict(line) == recorded


def test_the_log_keeps_exchanges_the_buffer_has_evicted(tmp_path):
    """Durability is the log's job; the buffer only bounds memory."""
    with ExchangeStore(tmp_path / "s", capacity=2) as store:
        for _ in range(5):
            store.record(captured())

        assert len(store.log_path.read_text().splitlines()) == 5
        assert len(store) == 2


def test_the_log_holds_faithful_unredacted_headers(store):
    """The store is not a redaction boundary. Serialization is where secrets stop."""
    store.record(captured())

    line = json.loads(store.log_path.read_text().splitlines()[0])

    assert [tuple(h) for h in line["request_headers"]] == [("Authorization", TOKEN)]


def test_an_errored_exchange_records_its_error(store):
    store.record(captured(t_end=None, status_code=None, error="connection refused"))

    line = json.loads(store.log_path.read_text().splitlines()[0])

    assert line["error"] == "connection refused"
    assert line["status_code"] is None


# --- the session directory ----------------------------------------------------


def test_the_session_and_bodies_directories_are_created(store):
    assert store.dir.is_dir()
    assert (store.dir / "bodies").is_dir()


def test_the_session_directory_is_private(store):
    """It holds unredacted secrets, so it is 0700 (SPEC.md, "Session directory")."""
    assert store.dir.stat().st_mode & 0o777 == 0o700


def test_a_nested_session_path_is_created(tmp_path):
    with ExchangeStore(tmp_path / "sessions" / "abc") as store:
        assert store.dir.is_dir()


# --- a late arrival after close -----------------------------------------------
#
# Found by the P3 live run. Connections still in flight when stop_capture runs deliver their
# error hook *after* the store is closed, and mitmproxy logs "Addon error: I/O operation on
# closed file". Nothing is lost that matters — the Session is over — but an exception thrown
# from an addon hook during shutdown is noise that looks like a defect, and on a busy capture
# it is one line per open connection.


def test_recording_after_close_is_ignored_rather_than_raising(tmp_path):
    """CaptureState.stop closes the store while the proxy may still be unwinding, so a late
    record is a normal part of shutdown, not a programming error."""
    store = ExchangeStore(tmp_path / "session")
    store.close()

    store.record(captured())

    assert store.recorded_count == 0


def test_late_arrivals_after_close_are_counted(tmp_path):
    """Counted rather than silent: "capture stopped while three connections were still open" is
    worth being able to see, and a silent drop is indistinguishable from never happening."""
    store = ExchangeStore(tmp_path / "session")
    store.close()

    store.record(captured())
    store.record(captured())

    assert store.dropped_after_close == 2


def test_a_tls_failure_after_close_is_ignored_too(tmp_path):
    """Same shutdown window, same hook problem."""
    store = ExchangeStore(tmp_path / "session")
    store.close()

    store.record_tls_failure("late.example", "connection reset")

    assert store.tls_failure_count == 0


def test_closing_twice_is_safe(tmp_path):
    """stop_capture runs on failure paths too, so it can reach a store that is already closed."""
    store = ExchangeStore(tmp_path / "session")
    store.close()
    store.close()
