"""Unit tests for the model-facing views: summaries, previews, slices, pagination.

Two properties matter more than the field lists, and both are asserted here.

A summary cannot carry a body. Not "does not by default" — there is no argument that would
make it, because the failure mode for an agent-driven traffic tool is flooding the context
window and the API should make that impossible rather than merely discouraged (SPEC.md).

Redaction happens *before* truncation and *before* slicing. Cutting first and redacting
after would leave the tail of a straddling secret in plain view, which is a leak dressed up
as a length limit.
"""

import base64
import json
import shlex

import pytest

from https_proxy_mcp.capture import BodyRef, Exchange
from https_proxy_mcp.serialize import digest
from https_proxy_mcp.serialize.views import (
    MAX_PAGE_SIZE,
    body_slice,
    content_type_of,
    curl_command,
    exchange_detail,
    exchange_summary,
    page_after,
)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def exchange(**overrides) -> Exchange:
    fields = {
        "id": 1,
        "t_start": 1000.0,
        "t_end": 1000.25,
        "method": "GET",
        "url": "http://api.example.com/me",
        "host": "api.example.com",
        "path": "/me",
        "scheme": "http",
        "http_version": "HTTP/1.1",
        "status_code": 200,
        "request_headers": [("Authorization", f"Bearer {TOKEN}")],
        "response_headers": [("Content-Type", "application/json")],
        "request_body": BodyRef.empty(),
        "response_body": BodyRef(size=8, stored_bytes=8, path="bodies/1.resp", truncated=False),
        "server_address": "93.184.216.34:80",
        "error": None,
    }
    return Exchange(**(fields | overrides))


# --- summaries ----------------------------------------------------------------


def test_a_summary_identifies_the_exchange():
    summary = exchange_summary(exchange())

    assert summary["id"] == 1
    assert summary["method"] == "GET"
    assert summary["host"] == "api.example.com"
    assert summary["path"] == "/me"
    assert summary["status"] == 200


def test_a_summary_reports_timing():
    summary = exchange_summary(exchange(t_start=1000.0, t_end=1000.25))

    assert summary["t_start"] == 1000.0
    assert summary["duration_ms"] == 250.0


def test_a_summary_reports_body_sizes_without_body_content():
    summary = exchange_summary(exchange())

    assert summary["response_bytes"] == 8
    assert summary["request_bytes"] == 0


def test_a_summary_cannot_carry_a_body():
    """The constraint is structural. If this ever fails, the design has been undone."""
    rendered = json.dumps(exchange_summary(exchange()))

    assert "preview" not in rendered
    assert "body" not in rendered.replace("_bytes", "")


def test_a_summary_omits_the_error_when_there_is_none():
    """Fifty rows of `"error": null` is pure context cost."""
    assert "error" not in exchange_summary(exchange())


def test_a_summary_reports_an_error_when_there_is_one():
    summary = exchange_summary(exchange(status_code=None, t_end=None, error="connection refused"))

    assert summary["error"] == "connection refused"
    assert summary["status"] is None
    assert summary["duration_ms"] is None


def test_a_summary_never_carries_a_header():
    """Headers hold secrets and are bulky; they belong in the detail view."""
    assert "request_headers" not in exchange_summary(exchange())


# --- detail -------------------------------------------------------------------


def test_detail_includes_the_full_request_line():
    detail = exchange_detail(exchange(), request_body=b"", response_body=b'{"id":1}')

    assert detail["url"] == "http://api.example.com/me"
    assert detail["http_version"] == "HTTP/1.1"
    assert detail["scheme"] == "http"
    assert detail["server_address"] == "93.184.216.34:80"


def test_detail_redacts_headers_by_default():
    detail = exchange_detail(exchange(), request_body=b"", response_body=b"")

    assert detail["request_headers"] == [["Authorization", digest(f"Bearer {TOKEN}")]]


def test_detail_can_include_secrets_on_request():
    detail = exchange_detail(exchange(), request_body=b"", response_body=b"", include_secrets=True)

    assert detail["request_headers"] == [["Authorization", f"Bearer {TOKEN}"]]


def test_detail_previews_a_body():
    detail = exchange_detail(exchange(), request_body=b"", response_body=b'{"id":1}')

    assert detail["response_body"]["preview"] == '{"id":1}'
    assert detail["response_body"]["truncated"] is False


def test_detail_redacts_a_body_preview():
    body = json.dumps({"access_token": "s3cret"}).encode()

    detail = exchange_detail(exchange(), request_body=b"", response_body=body)

    assert "s3cret" not in detail["response_body"]["preview"]


def test_a_body_is_redacted_before_it_is_truncated():
    """Truncating first would leave the tail of a straddling secret visible."""
    body = json.dumps({"padding": "x" * 40, "access_token": "s3cret"}).encode()

    detail = exchange_detail(exchange(), request_body=b"", response_body=body, max_body_bytes=50)

    assert "s3cret" not in detail["response_body"]["preview"]


def test_a_long_body_preview_is_truncated_and_says_so():
    body = b"x" * 5000

    preview = exchange_detail(exchange(), request_body=b"", response_body=body, max_body_bytes=100)[
        "response_body"
    ]

    assert preview["truncated"] is True
    assert preview["shown_bytes"] == 100
    assert "truncated" in preview["preview"]


def test_a_truncation_marker_names_the_tool_that_gets_the_rest():
    """A model that cannot see the way forward will guess or give up."""
    preview = exchange_detail(
        exchange(), request_body=b"", response_body=b"x" * 5000, max_body_bytes=100
    )["response_body"]

    assert "get_exchange_body" in preview["preview"]


def test_the_default_preview_is_two_kilobytes():
    preview = exchange_detail(exchange(), request_body=b"", response_body=b"x" * 5000)

    assert preview["response_body"]["shown_bytes"] == 2048


def test_a_binary_body_is_described_rather_than_dumped():
    """Base64 of a binary preview is thousands of tokens of noise."""
    preview = exchange_detail(exchange(), request_body=b"", response_body=bytes(range(256)))[
        "response_body"
    ]

    assert preview["encoding"] == "binary"
    assert preview["preview"] is None
    assert "get_exchange_body" in preview["note"]


def test_an_absent_body_is_reported_as_empty():
    preview = exchange_detail(exchange(), request_body=b"", response_body=b"")["request_body"]

    assert preview["size"] == 0
    assert preview["preview"] == ""


def test_detail_omits_the_error_when_there_is_none():
    assert "error" not in exchange_detail(exchange(), request_body=b"", response_body=b"")


# --- body slices --------------------------------------------------------------


def test_a_slice_returns_the_requested_window():
    result = body_slice(b"0123456789", offset=2, length=4)

    assert result["data"] == "2345"
    assert result["offset"] == 2
    assert result["length"] == 4


def test_a_slice_reports_whether_more_remains():
    assert body_slice(b"0123456789", offset=0, length=4)["has_more"] is True
    assert body_slice(b"0123456789", offset=6, length=4)["has_more"] is False


def test_a_slice_reports_the_total_it_is_slicing():
    assert body_slice(b"0123456789", offset=0, length=4)["total"] == 10


def test_a_slice_past_the_end_is_empty_rather_than_an_error():
    result = body_slice(b"0123", offset=99, length=10)

    assert result["data"] == ""
    assert result["has_more"] is False


def test_a_slice_is_taken_from_the_redacted_body():
    """Offsets index whatever representation you asked for, so they stay self-consistent."""
    body = json.dumps({"access_token": "s3cret"}).encode()

    result = body_slice(body, offset=0, length=1000)

    assert "s3cret" not in result["data"]
    assert result["total"] == len(result["data"])


def test_a_slice_of_a_binary_body_is_base64():
    """Here the caller explicitly asked for the bytes, so encoding them is the right answer."""
    blob = bytes([0xFF, 0xFE, 0x00, 0x01])

    result = body_slice(blob, offset=0, length=16)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["data"]) == blob


def test_a_slice_can_include_secrets():
    body = json.dumps({"access_token": "s3cret"}).encode()

    assert "s3cret" in body_slice(body, offset=0, length=1000, include_secrets=True)["data"]


def test_a_split_multibyte_character_does_not_break_a_slice():
    result = body_slice("café".encode(), offset=0, length=4)

    assert result["data"].startswith("caf")


# --- pagination ---------------------------------------------------------------


def test_a_page_returns_at_most_the_limit():
    items = [exchange(id=n) for n in range(1, 11)]

    page, _ = page_after(items, limit=3)

    assert [item.id for item in page] == [1, 2, 3]


def test_available_counts_every_row_past_the_marker():
    items = [exchange(id=n) for n in range(1, 11)]

    page, available = page_after(items, limit=3, after=3)

    assert [item.id for item in page] == [4, 5, 6]
    assert available == 7


def test_available_is_not_capped_by_the_page():
    """The count is how the caller tells "the end" from "more remains"."""
    items = [exchange(id=n) for n in range(1, 11)]

    _, available = page_after(items, limit=3)

    assert available == 10


def test_paging_through_everything_yields_each_item_once():
    items = [exchange(id=n) for n in range(1, 11)]
    seen, after = [], 0

    while after < items[-1].id:
        page, _ = page_after(items, limit=4, after=after)
        seen.extend(item.id for item in page)
        after = page[-1].id

    assert seen == list(range(1, 11))


def test_an_oversized_limit_is_clamped():
    items = [exchange(id=n) for n in range(1, 500)]

    page, _ = page_after(items, limit=10_000)

    assert len(page) == MAX_PAGE_SIZE


def test_a_nonsense_limit_is_rejected():
    with pytest.raises(ValueError, match="limit must be at least 1"):
        page_after([], limit=0)


def test_an_empty_listing_pages_to_nothing():
    assert page_after([], limit=10) == ([], 0)


# --- curl export --------------------------------------------------------------


def test_a_curl_command_reproduces_the_request():
    """Asserted through the shell's own parser, so this tests the command and not its quoting."""
    words = shlex.split(curl_command(exchange(), request_body=b""))

    assert words[:4] == ["curl", "-X", "GET", "http://api.example.com/me"]


def test_a_curl_command_quotes_a_url_that_needs_it():
    command = curl_command(exchange(url="http://x.test/s?a=1&b=2"), request_body=b"")

    assert "'http://x.test/s?a=1&b=2'" in command


def test_a_curl_command_carries_the_headers():
    command = curl_command(exchange(), request_body=b"")

    assert "-H 'Authorization: " in command


def test_a_curl_command_redacts_by_default():
    command = curl_command(exchange(), request_body=b"")

    assert TOKEN not in command
    assert digest(f"Bearer {TOKEN}") in command


def test_a_curl_command_can_be_made_runnable():
    """A redacted command does not run, so reproducing a request is an explicit opt-in."""
    command = curl_command(exchange(), request_body=b"", include_secrets=True)

    assert f"Bearer {TOKEN}" in command


def test_a_curl_command_includes_a_body():
    command = curl_command(exchange(method="POST"), request_body=b'{"a":1}', include_secrets=True)

    assert "--data-raw '{\"a\":1}'" in command


def test_a_curl_command_quotes_a_single_quote_safely():
    """Shell-quoting a value that contains a quote is how these commands get dangerous."""
    command = curl_command(
        exchange(url="http://x.test/a'b"), request_body=b"", include_secrets=True
    )

    assert "'a'b'" not in command


def test_a_binary_body_is_not_inlined_into_a_curl_command():
    command = curl_command(exchange(method="POST"), request_body=bytes(range(256)))

    assert "--data-raw" not in command
    assert "binary" in command


# --- content type -------------------------------------------------------------


def test_a_content_type_is_found_case_insensitively():
    assert content_type_of([("CONTENT-TYPE", "application/json")]) == "application/json"


def test_an_absent_content_type_is_none():
    assert content_type_of([("Accept", "*/*")]) is None
