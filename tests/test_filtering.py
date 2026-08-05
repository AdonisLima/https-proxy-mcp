"""Unit tests for filtering Exchanges with mitmproxy's own filter expression language.

Reusing that language rather than inventing a query syntax is a deliberate choice (SPEC.md):
it is documented, already tested, and already what the capture layer consumes. The cost is
that its matchers want a mitmproxy flow, and the store holds Exchanges — so an Exchange is
rebuilt into a flow to be matched, and these tests pin down that the translation is faithful.
"""

import pytest

from https_proxy_mcp.capture import BodyRef, Exchange
from https_proxy_mcp.capture.filtering import exchange_matcher


def exchange(**overrides) -> Exchange:
    fields = {
        "id": 1,
        "t_start": 1000.0,
        "t_end": 1000.25,
        "method": "POST",
        "url": "http://api.example.com/auth",
        "host": "api.example.com",
        "path": "/auth",
        "scheme": "http",
        "http_version": "HTTP/1.1",
        "status_code": 401,
        "request_headers": [("Content-Type", "application/json"), ("X-Trace", "abc")],
        "response_headers": [("Content-Type", "application/json")],
        "request_body": BodyRef(size=14, stored_bytes=14, path="bodies/1.req", truncated=False),
        "response_body": BodyRef(size=16, stored_bytes=16, path="bodies/1.resp", truncated=False),
        "server_address": "93.184.216.34:80",
        "error": None,
    }
    return Exchange(**(fields | overrides))


def bodies(request: bytes = b'{"user":"ada"}', response: bytes = b'{"error":"nope"}'):
    """A read_body callable, plus a record of what it was asked for."""
    reads: list[str] = []

    def read_body(_exchange: Exchange, side: str) -> bytes:
        reads.append(side)
        return request if side == "request" else response

    return read_body, reads


def matches(expression: str, exchange_under_test: Exchange | None = None) -> bool:
    read_body, _ = bodies()
    return exchange_matcher(expression, read_body)(exchange_under_test or exchange())


# --- metadata filters ---------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("~m POST", True),
        ("~m GET", False),
        ("~c 401", True),
        ("~c 200", False),
        ("~u /auth", True),
        ("~u /orders", False),
        ("~d api.example.com", True),
        ("~d other.example.com", False),
        ("~h X-Trace", True),
        ("~h X-Absent", False),
    ],
)
def test_metadata_filters_match_the_rebuilt_flow(expression, expected):
    assert matches(expression) is expected


def test_filters_compose_with_boolean_operators():
    assert matches("~m POST & ~c 401") is True
    assert matches("~m GET | ~c 401") is True
    assert matches("!~c 200") is True
    assert matches("~m GET & ~c 401") is False


def test_an_errored_exchange_has_no_status_to_match():
    errored = exchange(status_code=None, t_end=None, error="connection refused")

    assert matches("~c 401", errored) is False


def test_an_errored_exchange_still_matches_on_its_request():
    errored = exchange(status_code=None, t_end=None, error="connection refused")

    assert matches("~u /auth", errored) is True


def test_the_scheme_is_preserved_in_the_rebuilt_url():
    assert matches("~u https://", exchange(url="https://api.example.com/auth", scheme="https"))


# --- body filters -------------------------------------------------------------


def test_a_body_filter_matches_the_request_body():
    read_body, _ = bodies(request=b'{"user":"ada"}')

    assert exchange_matcher("~bq ada", read_body)(exchange()) is True


def test_a_body_filter_matches_the_response_body():
    read_body, _ = bodies(response=b'{"error":"nope"}')

    assert exchange_matcher("~bs nope", read_body)(exchange()) is True


def test_a_body_filter_matches_either_side():
    read_body, _ = bodies()

    assert exchange_matcher("~b ada", read_body)(exchange()) is True


def test_a_body_filter_that_does_not_match_is_false():
    read_body, _ = bodies()

    assert exchange_matcher("~b absent-string", read_body)(exchange()) is False


# --- bodies are only read when the expression needs them ----------------------


def test_a_metadata_filter_reads_no_bodies():
    """Bodies are on disk. Reading them for every Exchange on every search would be waste."""
    read_body, reads = bodies()

    exchange_matcher("~c 401", read_body)(exchange())

    assert reads == []


def test_a_body_filter_reads_the_bodies():
    read_body, reads = bodies()

    exchange_matcher("~b ada", read_body)(exchange())

    assert sorted(reads) == ["request", "response"]


# --- bad expressions ----------------------------------------------------------


def test_an_invalid_expression_is_rejected_when_it_is_compiled():
    """Rejected once at compile time, not per Exchange, and with the expression quoted."""
    read_body, _ = bodies()

    with pytest.raises(ValueError, match="filter"):
        exchange_matcher("~~~bogus(((", read_body)


def test_an_empty_expression_matches_everything():
    read_body, _ = bodies()

    assert exchange_matcher("", read_body)(exchange()) is True
