"""HAR 1.2 export: the artifact that leaves the machine.

Every other boundary in this project redacts on the way *into a model*. This one goes further — a
HAR file gets attached to a ticket, pasted into a thread, committed to a branch — so SPEC.md makes
``redact=True`` the default here as well. The consequence is the same one ``export_curl`` accepts:
the default output is not a faithful replay, and asking for one is an explicit act.

The file records **in itself** whether it was redacted. Working that out from the contents months
later is how a redacted export gets mistaken for evidence that no token was ever sent.

What comes back to the caller is a path and a count, never the HAR. A real export is megabytes, and
returning one would flood the context the whole progressive-disclosure design exists to protect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from https_proxy_mcp.capture.exchange import Exchange
from https_proxy_mcp.serialize.redact import redact_body, redact_headers
from https_proxy_mcp.serialize.views import content_type_of

HAR_VERSION = "1.2"

CREATOR_NAME = "https-proxy-mcp"

#: HAR has no representation for "no response arrived", and 0 is what every viewer reads as one.
NO_RESPONSE_STATUS = 0

#: HAR's own convention for a timing that is not known.
UNKNOWN_TIME = -1

REDACTED_COMMENT = (
    "Secrets are redacted: each is replaced by a stable <redacted:sha256:...> digest, so a"
    " repeated value is still recognisable as the same one. This file is NOT a faithful replay."
)

FAITHFUL_COMMENT = (
    "Exported with include_secrets: bodies and headers are verbatim and this file may contain"
    " live credentials. Treat it as a secret."
)

#: A body this size is described rather than embedded, so one video response cannot produce a
#: hundred-megabyte artifact.
MAX_EMBEDDED_BODY = 1024 * 1024


def write_har(
    exchanges: Iterable[Exchange],
    *,
    read_body: Callable[[Exchange, str], bytes],
    path: Path | str,
    redact: bool = True,
) -> dict[str, Any]:
    """Write these Exchanges as a HAR file, and report where it went.

    ``read_body`` is injected rather than the store being imported, for the reason the rest of
    :mod:`https_proxy_mcp.serialize` does no I/O: the store owns the spill layout, this module owns
    representation, and keeping them apart is what makes both testable alone.
    """
    entries = [_entry(item, read_body=read_body, redact=redact) for item in exchanges]

    document = {
        "log": {
            "version": HAR_VERSION,
            "creator": {"name": CREATOR_NAME, "version": HAR_VERSION},
            "entries": entries,
            "comment": REDACTED_COMMENT if redact else FAITHFUL_COMMENT,
        }
    }

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    # A faithful export holds real credentials, and even a redacted one holds full URLs. Same
    # reasoning as the Session directory being 0700.
    target.chmod(0o600)

    return {
        "path": str(target),
        "entries": len(entries),
        "redacted": redact,
    }


def _entry(
    exchange: Exchange, *, read_body: Callable[[Exchange, str], bytes], redact: bool
) -> dict[str, Any]:
    """One Exchange as a HAR entry."""
    include_secrets = not redact
    duration = exchange.duration_ms

    return {
        "startedDateTime": _iso(exchange.t_start),
        "time": round(duration, 3) if duration is not None else UNKNOWN_TIME,
        "request": _request(exchange, read_body=read_body, include_secrets=include_secrets),
        "response": _response(exchange, read_body=read_body, include_secrets=include_secrets),
        "cache": {},
        "timings": {
            # Only the total is known: the proxy sees a request and a response, not the phases
            # between them. Reporting invented phase timings would be worse than admitting it.
            "send": UNKNOWN_TIME,
            "wait": round(duration, 3) if duration is not None else UNKNOWN_TIME,
            "receive": UNKNOWN_TIME,
        },
        "serverIPAddress": (exchange.server_address or "").split(":")[0] or None,
        "comment": f"exchange {exchange.id}"
        + (f"; error: {exchange.error}" if exchange.error else ""),
    }


def _request(
    exchange: Exchange, *, read_body: Callable[[Exchange, str], bytes], include_secrets: bool
) -> dict[str, Any]:
    headers = _headers(exchange.request_headers, include_secrets)
    body = _body(
        exchange,
        "request",
        read_body=read_body,
        content_type=content_type_of(exchange.request_headers),
        include_secrets=include_secrets,
    )

    request: dict[str, Any] = {
        "method": exchange.method,
        "url": exchange.url,
        "httpVersion": exchange.http_version,
        "headers": headers,
        "queryString": _query(exchange.url),
        "cookies": [],
        "headersSize": UNKNOWN_TIME,
        "bodySize": exchange.request_body.size,
    }
    if body is not None:
        request["postData"] = {
            "mimeType": content_type_of(exchange.request_headers) or "",
            "text": body,
        }
    return request


def _response(
    exchange: Exchange, *, read_body: Callable[[Exchange, str], bytes], include_secrets: bool
) -> dict[str, Any]:
    body = _body(
        exchange,
        "response",
        read_body=read_body,
        content_type=content_type_of(exchange.response_headers),
        include_secrets=include_secrets,
    )

    return {
        "status": exchange.status_code if exchange.status_code is not None else NO_RESPONSE_STATUS,
        "statusText": "",
        "httpVersion": exchange.http_version,
        "headers": _headers(exchange.response_headers, include_secrets),
        "cookies": [],
        "content": {
            "size": exchange.response_body.size,
            "mimeType": content_type_of(exchange.response_headers) or "",
            **({"text": body} if body is not None else {}),
        },
        "redirectURL": "",
        "headersSize": UNKNOWN_TIME,
        "bodySize": exchange.response_body.size,
    }


def _body(
    exchange: Exchange,
    side: str,
    *,
    read_body: Callable[[Exchange, str], bytes],
    content_type: str | None,
    include_secrets: bool,
) -> str | None:
    """One side's body as text, redacted unless secrets were asked for.

    A body that cannot be read is *described* rather than fatal. A file removed by retention, or a
    Session directory that moved, must not cost the whole artifact — one missing body is a far
    smaller loss than no export at all.
    """
    try:
        raw = read_body(exchange, side)
    except OSError as exc:
        return f"<body unavailable: {exc}>"

    if not raw:
        return None
    if len(raw) > MAX_EMBEDDED_BODY:
        return f"<omitted: {len(raw)} bytes; read it with get_exchange_body>"

    rendered = redact_body(raw, content_type=content_type, include_secrets=include_secrets)
    try:
        return rendered.decode("utf-8")
    except UnicodeDecodeError:
        # HAR can carry base64, but a binary body in a diagnostic export is noise. Describing it
        # keeps the file readable and the bytes are still one tool call away.
        return f"<binary: {len(rendered)} bytes; read it with get_exchange_body>"


def _headers(headers: list[tuple[str, str]], include_secrets: bool) -> list[dict[str, str]]:
    """HAR wants name/value objects, and repeats are legitimate — `Set-Cookie` arrives twice."""
    return [
        {"name": name, "value": value}
        for name, value in redact_headers(headers, include_secrets=include_secrets)
    ]


def _query(url: str) -> list[dict[str, str]]:
    return [
        {"name": name, "value": value}
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    ]


def _iso(epoch_seconds: float) -> str:
    """HAR requires an offset. A naive `isoformat()` produces a file some viewers read as local
    time, which silently shifts every entry by the developer's timezone."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()
