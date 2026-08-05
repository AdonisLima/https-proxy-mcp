"""The mitmproxy addon: the one place that knows mitmproxy's shapes.

Everything downstream — the store, the correlator, the tool surface — sees only
:class:`CapturedExchange`. That containment is what keeps a mitmproxy upgrade a change to
this file rather than a change everywhere.

The store is injected. There is no mitmproxy option plumbing for the session directory,
because in production the MCP server owns the proxy in-process (ADR-0001) and can simply
hand over the store it already has.

``flow`` below is mitmproxy's object, not our Exchange. CONTEXT.md avoids the word for our
own concept precisely because it is overloaded — mitmproxy's request/response pair and
Maestro's test file — but at this boundary it is the foreign type's real name.
"""

from __future__ import annotations

from mitmproxy import http

from https_proxy_mcp.capture.exchange import CapturedExchange
from https_proxy_mcp.capture.store import ExchangeStore


class CaptureAddon:
    """Translates completed and failed mitmproxy flows into recorded Exchanges.

    Recording happens on completion rather than on request, so every Exchange carries a
    full interval. An Exchange that never completes still lands, via the error hook, with
    no end and no status — a request that got no response is frequently the whole
    diagnosis, so it is recorded rather than dropped.
    """

    def __init__(self, store: ExchangeStore) -> None:
        self._store = store

    # --- mitmproxy hooks ------------------------------------------------------

    def response(self, flow: http.HTTPFlow) -> None:
        """A response arrived and the Exchange is complete."""
        self._store.record(self._observe(flow))

    def error(self, flow: http.HTTPFlow) -> None:
        """The Exchange failed. Recorded with whatever is known, which is the request."""
        self._store.record(self._observe(flow))

    # --- translation ----------------------------------------------------------

    def _observe(self, flow: http.HTTPFlow) -> CapturedExchange:
        response = flow.response
        return CapturedExchange(
            t_start=flow.request.timestamp_start,
            t_end=response.timestamp_end if response else None,
            method=flow.request.method,
            url=flow.request.url,
            # pretty_host prefers the Host header, which is the name the app asked for.
            host=flow.request.pretty_host,
            path=flow.request.path,
            scheme=flow.request.scheme,
            http_version=flow.request.http_version,
            status_code=response.status_code if response else None,
            request_headers=list(flow.request.headers.items(True)),
            response_headers=list(response.headers.items(True)) if response else [],
            request_body=_body(flow.request),
            response_body=_body(response) if response else b"",
            server_address=_address(flow),
            error=flow.error.msg if flow.error else None,
        )


def _body(message: http.Message) -> bytes:
    """The decoded body, falling back to the raw bytes rather than raising.

    Decoded because a gzipped blob on disk answers no debugging question. Non-strict
    because a single malformed Content-Encoding must not take down capture for the rest of
    the Session — mitmproxy's strict mode raises on one, and losing the whole Session to a
    misbehaving endpoint would be a poor trade.
    """
    return message.get_content(strict=False) or b""


def _address(flow: http.HTTPFlow) -> str | None:
    """The peer actually connected to, as ``host:port``.

    Worth keeping distinct from the requested host: in regular proxy mode this is the only
    record of where the traffic really went.
    """
    peername = flow.server_conn.peername
    if peername is None:
        return None
    return f"{peername[0]}:{peername[1]}"
