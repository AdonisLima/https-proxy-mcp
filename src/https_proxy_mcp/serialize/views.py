"""Shaping Exchanges for a model: summaries, previews, slices, pagination, curl.

Does no I/O. Bodies arrive as bytes, because the store owns the spill layout and this module
owns representation; keeping the two apart is what makes both testable alone. Pagination is
pure arithmetic over Exchange ids — a page and a count of what remains past a marker, with no
cursor-string knowledge. What a cursor *is*, what a caller is told at the end of a listing,
and whether a nonsense limit is refused or clamped are tool-surface policies, and they live at
the tool surface.

Two invariants hold throughout, and both are structural rather than advisory:

**A summary cannot carry a body.** Not "does not by default" — :func:`exchange_summary`
takes no body argument, so there is nothing to pass. The failure mode for an agent-driven
traffic tool is flooding the context window, and the API should make that impossible rather
than merely discouraged (SPEC.md, "Progressive disclosure is enforced by the API shape").

**Redaction precedes truncation and slicing.** Cutting first would leave the tail of a
secret that straddles the boundary in plain view, which is a leak wearing a length limit as
a disguise. The cost is that the whole body is redacted to show 2 KB of it; correctness is
worth more than the cycles, and bodies are capped at 10 MB anyway.
"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Iterable, Sequence
from typing import Any

from https_proxy_mcp.capture.exchange import Exchange
from https_proxy_mcp.serialize.redact import redact_body, redact_headers

#: Body bytes shown inline by ``get_exchange`` (SPEC.md).
DEFAULT_PREVIEW_BYTES = 2048

#: Body bytes returned per ``get_exchange_body`` call.
DEFAULT_SLICE_BYTES = 64 * 1024

#: Exchanges per page, and the ceiling an oversized request is clamped to.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# --- summaries ----------------------------------------------------------------


def exchange_summary(exchange: Exchange) -> dict[str, Any]:
    """One Exchange as a listing row: enough to choose one, never enough to flood.

    Headers are absent as well as bodies. They are bulky, they hold secrets, and choosing
    *which* Exchange to look at never needs them.
    """
    summary: dict[str, Any] = {
        "id": exchange.id,
        "t_start": round(exchange.t_start, 3),
        "duration_ms": _rounded(exchange.duration_ms),
        "method": exchange.method,
        "host": exchange.host,
        "path": exchange.path,
        "status": exchange.status_code,
        "request_bytes": exchange.request_body.size,
        "response_bytes": exchange.response_body.size,
    }
    if exchange.error is not None:
        # Omitted when absent: fifty rows of `"error": null` is pure context cost.
        summary["error"] = exchange.error
    return summary


# --- detail -------------------------------------------------------------------


def exchange_detail(
    exchange: Exchange,
    *,
    request_body: bytes,
    response_body: bytes,
    max_body_bytes: int = DEFAULT_PREVIEW_BYTES,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """One Exchange in full, with both bodies previewed rather than dumped."""
    detail: dict[str, Any] = {
        "id": exchange.id,
        "t_start": round(exchange.t_start, 3),
        "duration_ms": _rounded(exchange.duration_ms),
        "method": exchange.method,
        "url": exchange.url,
        "host": exchange.host,
        "path": exchange.path,
        "scheme": exchange.scheme,
        "http_version": exchange.http_version,
        "status": exchange.status_code,
        "server_address": exchange.server_address,
        "request_headers": _headers(exchange.request_headers, include_secrets),
        "response_headers": _headers(exchange.response_headers, include_secrets),
        "request_body": body_preview(
            request_body,
            content_type=content_type_of(exchange.request_headers),
            max_bytes=max_body_bytes,
            include_secrets=include_secrets,
        ),
        "response_body": body_preview(
            response_body,
            content_type=content_type_of(exchange.response_headers),
            max_bytes=max_body_bytes,
            include_secrets=include_secrets,
        ),
    }
    if exchange.error is not None:
        detail["error"] = exchange.error
    return detail


def body_preview(
    body: bytes,
    *,
    content_type: str | None = None,
    max_bytes: int = DEFAULT_PREVIEW_BYTES,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """A body's opening bytes, redacted first and then cut.

    A body that is not UTF-8 is *described*, not encoded: base64 of a 2 KB binary preview is
    thousands of tokens of noise nobody can read. Asking for the bytes explicitly, via
    :func:`body_slice`, gets them.
    """
    rendered = redact_body(body, content_type=content_type, include_secrets=include_secrets)

    if not rendered:
        return {"size": 0, "shown_bytes": 0, "truncated": False, "encoding": "text", "preview": ""}

    if not _is_text(rendered):
        return {
            "size": len(rendered),
            "shown_bytes": 0,
            "truncated": False,
            "encoding": "binary",
            "preview": None,
            "note": "not UTF-8 text; use get_exchange_body for the raw bytes",
        }

    shown = rendered[:max_bytes]
    truncated = len(shown) < len(rendered)
    text = shown.decode("utf-8", errors="replace")
    if truncated:
        text += (
            f"\n<truncated: showing {len(shown)} of {len(rendered)} bytes;"
            f" use get_exchange_body for the rest>"
        )

    return {
        "size": len(rendered),
        "shown_bytes": len(shown),
        "truncated": truncated,
        "encoding": "text",
        "preview": text,
    }


# --- slices -------------------------------------------------------------------


def body_slice(
    body: bytes,
    *,
    offset: int = 0,
    length: int = DEFAULT_SLICE_BYTES,
    content_type: str | None = None,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """A window into a body.

    Offsets index the representation that was asked for: redact first, then slice, so
    ``offset`` and ``total`` stay consistent with each other across a whole paged read.
    Slicing the raw bytes and redacting afterwards would be cheaper and would leak a secret
    lying across a boundary.
    """
    rendered = redact_body(body, content_type=content_type, include_secrets=include_secrets)
    window = rendered[offset : offset + length]

    return {
        "offset": offset,
        "length": len(window),
        "total": len(rendered),
        "has_more": offset + len(window) < len(rendered),
        # The caller asked for bytes here, so encoding them is the right answer rather than
        # the noise it would be in a preview.
        "encoding": "text" if _is_text(rendered) else "base64",
        "data": (
            window.decode("utf-8", errors="replace")
            if _is_text(rendered)
            else base64.b64encode(window).decode("ascii")
        ),
    }


# --- pagination ---------------------------------------------------------------


def page_after(
    exchanges: Sequence[Exchange], *, limit: int = DEFAULT_PAGE_SIZE, after: int = 0
) -> tuple[list[Exchange], int]:
    """One page of Exchanges, plus how many rows sit past ``after`` at all.

    Ids are monotonic, so resuming is "everything after this id" — no offset to go stale as
    the ring buffer evicts underneath a paging agent. ``available`` counts every row past
    ``after``, paged or not, so the caller can tell "that was the end" from "more remains"
    without a second call. Where ``after`` comes from and what the caller does with the count
    are the caller's policies, not this function's.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    limit = min(limit, MAX_PAGE_SIZE)

    remaining = [exchange for exchange in exchanges if exchange.id > after]
    return remaining[:limit], len(remaining)


# --- curl export --------------------------------------------------------------


def curl_command(exchange: Exchange, *, request_body: bytes, include_secrets: bool = False) -> str:
    """The Exchange as a ``curl`` command line.

    Redacted by default, like everything else that reaches a model — which means the default
    does not run. Reproducing a request is therefore an explicit ``include_secrets`` opt-in
    rather than a surprise.
    """
    parts = ["curl", "-X", exchange.method, shlex.quote(exchange.url)]

    for name, value in _headers(exchange.request_headers, include_secrets):
        parts += ["-H", shlex.quote(f"{name}: {value}")]

    rendered = redact_body(
        request_body,
        content_type=content_type_of(exchange.request_headers),
        include_secrets=include_secrets,
    )
    if rendered:
        if _is_text(rendered):
            parts += ["--data-raw", shlex.quote(rendered.decode("utf-8"))]
        else:
            # Inlining megabytes of binary into a shell command helps nobody.
            parts.append(f"# body omitted: {len(rendered)} bytes of binary")

    return " ".join(parts)


# --- helpers ------------------------------------------------------------------


def content_type_of(headers: Iterable[tuple[str, str]]) -> str | None:
    """The Content-Type value, matched case-insensitively, or ``None``."""
    for name, value in headers:
        if name.lower() == "content-type":
            return value
    return None


def _headers(headers: Sequence[tuple[str, str]], include_secrets: bool) -> list[list[str]]:
    """Redacted header pairs as JSON-native lists, keeping order and repeats."""
    return [
        [name, value] for name, value in redact_headers(headers, include_secrets=include_secrets)
    ]


def _is_text(body: bytes) -> bool:
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
