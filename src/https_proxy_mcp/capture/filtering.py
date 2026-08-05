"""Filtering Exchanges with mitmproxy's own filter expression language.

Reusing that language rather than inventing a query syntax is deliberate (SPEC.md): it is
documented, already tested, and already what the capture layer consumes. It also means an
agent that knows mitmproxy knows this tool's search syntax already.

The cost is an impedance mismatch. mitmproxy's matchers want a flow, and the store keeps
Exchanges with their bodies on disk — so an Exchange is rebuilt into a flow in order to be
matched. Rebuilding is cheap for metadata and expensive for bodies, which is why bodies are
fetched only when the expression actually mentions one.
"""

from __future__ import annotations

from collections.abc import Callable

from mitmproxy import flowfilter
from mitmproxy.connection import Client, Server
from mitmproxy.http import HTTPFlow, Request, Response

from https_proxy_mcp.capture.exchange import Exchange

#: A callable that produces one side's body for an Exchange — normally ``store.read_body``.
ReadBody = Callable[[Exchange, str], bytes]

#: Every body-matching operator in the filter language (``~b``, ``~bq``, ``~bs``) starts
#: with this. Testing the expression text is crude, but it errs toward reading bodies that
#: were not needed rather than skipping bodies that were.
_BODY_OPERATOR = "~b"


def exchange_matcher(expression: str, read_body: ReadBody) -> Callable[[Exchange], bool]:
    """Compile a filter expression into a predicate over Exchanges.

    Compiled once, so an invalid expression is rejected here rather than per Exchange. An
    empty expression matches everything, which is what makes ``filter`` an optional
    parameter on the tools that take one.
    """
    if not expression:
        return lambda _exchange: True

    try:
        matcher = flowfilter.parse(expression)
    except ValueError as exc:
        raise ValueError(f"invalid filter expression {expression!r}: {exc}") from None

    needs_bodies = _BODY_OPERATOR in expression

    def matches(exchange: Exchange) -> bool:
        request_body = read_body(exchange, "request") if needs_bodies else b""
        response_body = read_body(exchange, "response") if needs_bodies else b""
        return bool(matcher(as_flow(exchange, request_body, response_body)))

    return matches


def as_flow(exchange: Exchange, request_body: bytes = b"", response_body: bytes = b"") -> HTTPFlow:
    """Rebuild a mitmproxy flow from an Exchange, for filtering only.

    Enough of a flow to be matched, not a faithful reconstruction: this is never replayed
    and never written back. An Exchange that never completed gets no response, so a
    status-code filter correctly declines to match it rather than matching a fabricated 0.
    """
    # The connection tuples exist because HTTPFlow requires them; no filter operator reads
    # them. The request's own port comes from its URL, which Request.make parses.
    client = Client(
        peername=("127.0.0.1", 0), sockname=("127.0.0.1", 0), timestamp_start=exchange.t_start
    )
    flow = HTTPFlow(client, Server(address=(exchange.host, 0)))

    flow.request = Request.make(
        exchange.method,
        exchange.url,
        request_body,
        _headers(exchange.request_headers),
    )
    flow.request.timestamp_start = exchange.t_start

    if exchange.status_code is not None:
        flow.response = Response.make(
            exchange.status_code,
            response_body,
            _headers(exchange.response_headers),
        )
        if exchange.t_end is not None:
            flow.response.timestamp_end = exchange.t_end

    return flow


def _headers(headers: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """Header pairs as mitmproxy wants them, keeping order and repeats."""
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers]
