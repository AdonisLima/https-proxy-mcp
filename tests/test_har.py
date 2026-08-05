"""HAR export: the artifact that leaves the machine, and therefore redacts by default.

Every other boundary in this project redacts on the way to a model. A HAR file is the one that
travels further than that — into a bug report, a ticket, a chat thread — so SPEC.md makes
`redact=True` the default here too. The consequence is the same as `export_curl`'s: the default
output is not a faithful replay, and asking for one is explicit.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from https_proxy_mcp.capture.exchange import BodyRef, Exchange
from https_proxy_mcp.serialize.har import HAR_VERSION, write_har


def exch(
    exchange_id: int = 1,
    *,
    method: str = "GET",
    host: str = "api.example.com",
    path: str = "/thing",
    status_code: int | None = 200,
    request_headers=None,
    response_headers=None,
    request_size: int = 0,
    response_size: int = 0,
) -> Exchange:
    def body(size: int) -> BodyRef:
        return BodyRef(
            size=size, stored_bytes=size, path="x" if size else None, truncated=False
        )

    return Exchange(
        id=exchange_id,
        t_start=1785965701.868,
        t_end=1785965702.531,
        method=method,
        url=f"https://{host}{path}",
        host=host,
        path=path,
        scheme="https",
        http_version="HTTP/1.1",
        status_code=status_code,
        request_headers=request_headers or [("Accept", "application/json")],
        response_headers=response_headers or [("Content-Type", "application/json")],
        request_body=body(request_size),
        response_body=body(response_size),
    )


def bodies(request: bytes = b"", response: bytes = b""):
    def read(exchange: Exchange, side: str) -> bytes:
        return request if side == "request" else response

    return read


@pytest.fixture
def target(tmp_path):
    return tmp_path / "export.har"


def written(target) -> dict:
    return json.loads(target.read_text())["log"]


# --- the shape ----------------------------------------------------------------


def test_the_export_is_har_1_2(target):
    write_har([exch()], read_body=bodies(), path=target)

    log = written(target)
    assert log["version"] == HAR_VERSION == "1.2"
    assert log["creator"]["name"] == "https-proxy-mcp"


def test_one_entry_per_exchange(target):
    write_har([exch(1), exch(2)], read_body=bodies(), path=target)

    assert len(written(target)["entries"]) == 2


def test_an_entry_carries_the_request_and_the_response(target):
    write_har([exch(method="POST", status_code=201)], read_body=bodies(), path=target)

    entry = written(target)["entries"][0]
    assert entry["request"]["method"] == "POST"
    assert entry["request"]["url"] == "https://api.example.com/thing"
    assert entry["response"]["status"] == 201
    assert entry["time"] == pytest.approx(663, abs=1)


def test_the_start_time_is_iso_8601_with_a_timezone(target):
    """HAR's own requirement, and the reason a naive `isoformat()` produces a file some viewers
    silently misread as local time."""
    write_har([exch()], read_body=bodies(), path=target)

    started = written(target)["entries"][0]["startedDateTime"]
    assert started.startswith("2026-")
    assert started.endswith("+00:00") or started.endswith("Z")


def test_query_strings_are_parsed_out(target):
    write_har([exch(path="/search?q=hello&page=2")], read_body=bodies(), path=target)

    query = written(target)["entries"][0]["request"]["queryString"]
    assert {"name": "q", "value": "hello"} in query
    assert {"name": "page", "value": "2"} in query


def test_an_exchange_that_never_completed_still_exports(target):
    """A request with no response is frequently the whole diagnosis, so it must not be dropped
    from the artifact that gets attached to the bug report.

    HAR has no way to say "no response", so status 0 is the convention every viewer understands.
    """
    write_har([replace(exch(), status_code=None, t_end=None)], read_body=bodies(), path=target)

    entries = written(target)["entries"]
    assert len(entries) == 1
    assert entries[0]["response"]["status"] == 0
    assert entries[0]["time"] == -1


# --- redaction ----------------------------------------------------------------


def test_secrets_in_headers_are_redacted_by_default(target):
    write_har(
        [exch(request_headers=[("Authorization", "Bearer sk-secret-value")])],
        read_body=bodies(),
        path=target,
    )

    rendered = target.read_text()
    assert "sk-secret-value" not in rendered
    assert "redacted:sha256:" in rendered


def test_secrets_in_bodies_are_redacted_by_default(target):
    write_har(
        [exch(method="POST", request_size=40)],
        read_body=bodies(request=b'{"password": "hunter2"}'),
        path=target,
    )

    assert "hunter2" not in target.read_text()


def test_include_secrets_produces_a_faithful_export(target):
    """The explicit opt-out. Reproducing a request is a real need; it just is not the default."""
    write_har(
        [exch(request_headers=[("Authorization", "Bearer sk-secret-value")])],
        read_body=bodies(),
        path=target,
        redact=False,
    )

    assert "sk-secret-value" in target.read_text()


def test_the_export_records_whether_it_was_redacted(target):
    """Whoever opens the file months later needs to know whether they are looking at the real
    traffic or at digests — and guessing from the contents is how a redacted export gets
    mistaken for evidence that no token was sent."""
    write_har([exch()], read_body=bodies(), path=target)

    assert written(target)["comment"] and "redacted" in written(target)["comment"]


# --- what comes back ----------------------------------------------------------


def test_the_return_value_is_a_path_and_a_count_not_the_har(target):
    """A HAR is megabytes. Returning it would flood the context the tool exists to protect."""
    result = write_har([exch(1), exch(2)], read_body=bodies(), path=target)

    assert result["path"] == str(target)
    assert result["entries"] == 2
    assert result["redacted"] is True
    assert "log" not in result
    assert not any(isinstance(value, (list, dict)) for value in result.values())


def test_the_parent_directory_is_created_if_it_is_missing(target, tmp_path):
    nested = tmp_path / "exports" / "deep" / "out.har"

    write_har([exch()], read_body=bodies(), path=nested)

    assert nested.exists()


def test_the_export_is_private(target):
    """A faithful export holds real credentials. Same reasoning as the Session directory."""
    write_har([exch()], read_body=bodies(), path=target, redact=False)

    assert target.stat().st_mode & 0o777 == 0o600


def test_an_unreadable_body_does_not_end_the_export(target):
    """A body file removed by retention, or a Session directory moved. One missing body must not
    cost the whole artifact."""

    def explode(exchange, side):
        raise OSError("gone")

    result = write_har([exch(request_size=10)], read_body=explode, path=target)

    assert result["entries"] == 1
