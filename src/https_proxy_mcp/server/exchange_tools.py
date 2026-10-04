"""The read-only Exchange tools: list, get, slice, search, tail, wait, export.

Progressive disclosure is the organising idea and it is enforced by these signatures, not by
guidance. ``list_exchanges`` has no parameter that would return a body; a body needs a second
call, and a large one needs an offset (SPEC.md). An agent cannot flood its own context here
by accident.

Every listing reports ``evicted``, because "there is nothing older" and "there was something
older and it is gone" are different answers and only one of them means stop looking.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture import Exchange
from https_proxy_mcp.capture.filtering import exchange_matcher
from https_proxy_mcp.serialize.views import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_PREVIEW_BYTES,
    DEFAULT_SLICE_BYTES,
    MAX_PAGE_SIZE,
    body_slice,
    content_type_of,
    curl_command,
    exchange_detail,
    exchange_summary,
    page_after,
)
from https_proxy_mcp.server.state import CaptureState

#: ``wait_for_exchanges`` defaults and ceiling (SPEC.md).
DEFAULT_WAIT_MS = 15_000
MAX_WAIT_MS = 120_000

#: How often a wait re-checks. Pull-based by design (ADR-0005); this is the pull interval.
WAIT_POLL_S = 0.02

SIDES = ("request", "response")


def register_exchange_tools(server: FastMCP, state: CaptureState) -> None:
    """Register the tools that read captured traffic."""

    @server.tool()
    def list_exchanges(
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        host: str | None = None,
        method: str | None = None,
        status: int | None = None,
        since: float | None = None,
        until: float | None = None,
        include_denied: bool = False,
    ) -> dict[str, Any]:
        """List captured Exchanges, newest last, one compact row each.

        Deliberately cannot return bodies — call ``get_exchange`` for one Exchange in detail,
        or ``get_exchange_body`` for its bytes. Narrow with ``host`` (exact, case-insensitive),
        ``method``, ``status``, or the ``since``/``until`` epoch-second window. Page with
        ``cursor`` from the previous response; a ``next_cursor`` of null means that was the end.

        OS chatter — Apple and iCloud domains, revocation checks, telemetry ingest — is left
        out, and ``include_denied=true`` puts it back. The Session's ``scope`` is left out too and
        this parameter does **not** lift that: widen the Scope by starting a new Session.
        ``hidden`` says how many rows were removed — both together by default, and the Scope's alone
        when ``include_denied`` is true, since the denylist is then not applied.

        A row that is hidden was still captured, so ``get_exchange`` will fetch it by id — unless
        Passthrough kept it from being captured at all, which is what ``diagnose_capture`` says.
        """
        matching = _narrow(
            list(state.store), host=host, method=method, status=status, since=since, until=until
        )
        return _page(state, matching, limit=limit, cursor=cursor, include_denied=include_denied)

    @server.tool()
    def get_exchange(
        exchange_id: int,
        max_body_bytes: int = DEFAULT_PREVIEW_BYTES,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """One Exchange in full: headers, timing, and the opening bytes of each body.

        Secrets are replaced with a stable ``<redacted:sha256:…>`` digest — the same secret
        always renders to the same marker, so "this is the same token as before" and "the
        token changed after refresh" are answerable without the value being shown. Pass
        ``include_secrets=true`` only when the value itself is the question.
        """
        exchange = state.exchange(exchange_id)
        store = state.store
        return exchange_detail(
            exchange,
            request_body=store.read_body(exchange, "request"),
            response_body=store.read_body(exchange, "response"),
            max_body_bytes=max_body_bytes,
            include_secrets=include_secrets,
        )

    @server.tool()
    def get_exchange_body(
        exchange_id: int,
        side: str,
        offset: int = 0,
        length: int = DEFAULT_SLICE_BYTES,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """A window of one Exchange's body. ``side`` is "request" or "response".

        Offsets index the *redacted* body, so they stay consistent across a paged read.
        Keep reading while ``has_more`` is true, advancing ``offset`` by ``length``. A body
        that is not text comes back base64-encoded, flagged by ``encoding``.
        """
        if side not in SIDES:
            raise ToolError(f"side must be one of {', '.join(SIDES)}, not {side!r}")

        exchange = state.exchange(exchange_id)
        headers = exchange.request_headers if side == "request" else exchange.response_headers
        return {
            "exchange_id": exchange_id,
            "side": side,
            **body_slice(
                state.store.read_body(exchange, side),
                offset=offset,
                length=length,
                content_type=content_type_of(headers),
                include_secrets=include_secrets,
            ),
        }

    @server.tool()
    def search_exchanges(
        filter: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        include_denied: bool = False,
    ) -> dict[str, Any]:
        """Search Exchanges with a mitmproxy filter expression.

        This is mitmproxy's own language, not a new one: ``~u /auth`` (URL), ``~m POST``
        (method), ``~c 401`` (status), ``~d api.example.com`` (domain), ``~h`` (header),
        ``~b`` / ``~bq`` / ``~bs`` (body, either/request/response), combined with ``&``,
        ``|`` and ``!``. Body operators read from disk, so prefer a metadata filter when it
        will do.
        """
        # Compiled before the loop, not inside it: an invalid expression must be reported
        # even when there is no traffic to test it against, and recompiling per Exchange
        # would pay the parser cost thousands of times over.
        matches = _matcher(state, filter)
        matching = [item for item in state.store if matches(item)]
        return _page(
            state,
            matching,
            limit=limit,
            cursor=cursor,
            include_denied=include_denied,
            filter=filter,
        )

    @server.tool()
    def tail_exchanges(
        cursor: str | None = None,
        filter: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        include_denied: bool = False,
    ) -> dict[str, Any]:
        """Everything captured since ``cursor``, for watching traffic as it happens.

        Unlike ``list_exchanges``, the returned ``next_cursor`` is always usable — more
        traffic is expected, so an empty result hands back the cursor you gave it. Call again
        with it to pick up whatever arrived in between. To *block* until something specific
        shows up, use ``wait_for_exchanges`` instead of looping on this.

        Narrowed by the denylist and the Session's Scope, like ``list_exchanges``.
        """
        matches = _matcher(state, filter or "")
        listing = _listing(
            state,
            [item for item in state.store if matches(item)],
            # The one divergence, kept: a listing refuses a nonsense limit, a tail clamps it.
            limit=max(1, limit),
            cursor=cursor,
            include_denied=include_denied,
        )

        return {
            "exchanges": [exchange_summary(item) for item in listing.page],
            "returned": len(listing.page),
            # The tail's own population: only rows the cursor's owner has not seen count.
            "hidden": listing.hidden_fresh,
            "limit": listing.limit,
            "next_cursor": str(listing.page[-1].id) if listing.page else cursor,
            "more_available": len(listing.page) < listing.available,
            "evicted": listing.evicted,
            "recorded": listing.recorded,
        }

    @server.tool()
    async def wait_for_exchanges(
        filter: str,
        timeout_ms: int = DEFAULT_WAIT_MS,
        min_count: int = 1,
    ) -> dict[str, Any]:
        """Block until ``min_count`` *new* matching Exchanges arrive, or the timeout passes.

        This is what makes end-to-end use practical: "tap Login, then wait for POST /auth" is
        one call rather than a polling loop.

        Only traffic recorded *after* this call counts. An identical request a moment earlier
        will not satisfy the wait — otherwise "wait for the login request" would be answered
        by the previous login. A timeout is a result, not an error: ``timed_out`` is true and
        the answer is that it never happened.

        Exempt from the Scope and the denylist, and **not** exempt from the Lens. The exemption
        exists because naming a host in a filter is explicit and should not be vetoed by a
        narrowing the agent set earlier; a Lens is the case where that reasoning inverts. Its
        subject is which client the traffic came from, and a wait that ignored it is answered by
        the other simulator's login — the same defect the "recorded after this call" rule above
        exists to prevent, arriving by a different route.
        """
        timeout_ms = max(1, min(timeout_ms, MAX_WAIT_MS))
        matches = _matcher(state, filter)
        lens = state.lens_predicate
        # Only Exchanges recorded after this instant can satisfy the wait.
        after = state.store.recorded_count
        deadline = time.monotonic() + timeout_ms / 1000

        found: list[Exchange] = []
        while time.monotonic() < deadline:
            found = [
                item
                for item in state.store
                if item.id > after and (lens is None or lens(item)) and matches(item)
            ]
            if len(found) >= min_count:
                break
            await asyncio.sleep(WAIT_POLL_S)

        return {
            "timed_out": len(found) < min_count,
            "timeout_ms": timeout_ms,
            "filter": filter,
            # Named beside the filter because a timeout is the result that most needs explaining,
            # and "it did happen, the Lens excluded it" is otherwise indistinguishable from "it
            # never happened" — which is the whole answer this tool exists to give.
            "lens": state.lens,
            "min_count": min_count,
            "returned": len(found),
            "exchanges": [exchange_summary(item) for item in found],
            "evicted": state.store.evicted_count,
        }

    @server.tool()
    def export_curl(exchange_id: int, include_secrets: bool = False) -> dict[str, Any]:
        """Render one Exchange as a ``curl`` command.

        Redacted by default, like everything else that reaches a model — which means the
        default will not run. Pass ``include_secrets=true`` for a command that actually
        reproduces the request.
        """
        exchange = state.exchange(exchange_id)
        return {
            "exchange_id": exchange_id,
            "runnable": include_secrets,
            "command": curl_command(
                exchange,
                request_body=state.store.read_body(exchange, "request"),
                include_secrets=include_secrets,
            ),
        }


# --- helpers ------------------------------------------------------------------


def _matcher(state: CaptureState, expression: str) -> Callable[[Exchange], bool]:
    """Compile a filter, translating a bad expression into something an agent can fix."""
    try:
        return exchange_matcher(expression, state.store.read_body)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _narrow(
    exchanges: list[Exchange],
    *,
    host: str | None,
    method: str | None,
    status: int | None,
    since: float | None,
    until: float | None,
) -> list[Exchange]:
    """Apply the cheap structured narrowings, in the order they were given."""
    if host is not None:
        wanted = host.lower()
        exchanges = [item for item in exchanges if item.host.lower() == wanted]
    if method is not None:
        wanted = method.upper()
        exchanges = [item for item in exchanges if item.method.upper() == wanted]
    if status is not None:
        exchanges = [item for item in exchanges if item.status_code == status]
    if since is not None:
        exchanges = [item for item in exchanges if item.t_start >= since]
    if until is not None:
        exchanges = [item for item in exchanges if item.t_start <= until]
    return exchanges


@dataclass(frozen=True, slots=True)
class _Listing:
    """What every browse tool computes the same way: the page, and the counts around it.

    ``hidden`` and ``hidden_fresh`` are deliberately both here, because they are two
    populations and each browse tool owes its caller a different one. A listing counts the
    whole matching set — rows before the cursor were matched and hidden too, and dropping them
    from the count would read as "there was no older traffic". A tail counts only rows from the
    cursor onward, because rows before it are ones the caller has already been offered.
    Neither is derivable from the other, so the core computes both and each wrapper reports
    its own.
    """

    page: list[Exchange]
    available: int
    matched: int
    hidden: int
    hidden_fresh: int
    limit: int
    evicted: int
    recorded: int


def _listing(
    state: CaptureState,
    exchanges: list[Exchange],
    *,
    limit: int,
    cursor: str | None,
    include_denied: bool = False,
) -> _Listing:
    """The one pipeline every browse tool goes through: parse, narrow, clamp, page, count.

    This directory's trap file records what happens when a browse tool assembles this inline
    instead: the limit ceiling and the denylist narrowing were each bypassed exactly once,
    both times by the one tool that did not share the pipeline. A future browse tool goes
    through here, or the invariant is maintained by hand again.

    The reporting narrowing — Run window, denylist, Scope, Lens — comes from
    :meth:`CaptureState.reporting`, so ``include_denied`` and the Session's Lens behave exactly
    as they do for every other caller. A bad cursor raises :class:`ToolError` here; a nonsense
    ``limit`` raises ``ValueError`` out of :func:`page_after`, and whether that becomes a
    refusal or a clamp is the wrapper's own policy.
    """
    after = _parse_cursor(cursor)
    reported, hidden = state.reporting(include_denied=include_denied).apply(exchanges)
    page, available = page_after(reported, limit=limit, after=after)
    fresh = sum(1 for item in exchanges if item.id > after)
    store = state.store
    return _Listing(
        page=page,
        available=available,
        matched=len(reported),
        hidden=hidden,
        hidden_fresh=fresh - available,
        limit=min(limit, MAX_PAGE_SIZE),
        evicted=store.evicted_count,
        recorded=store.recorded_count,
    )


def _page(
    state: CaptureState,
    exchanges: list[Exchange],
    *,
    limit: int,
    cursor: str | None,
    include_denied: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """One page of summaries, with the counts every listing owes its caller.

    A thin wrapper over :func:`_listing` — what stays here is what is a listing's alone: the
    full-set ``hidden``, ``matched``, the policy that the end of a listing offers no cursor,
    and the refusal of a nonsense ``limit`` where a tail would clamp.
    """
    try:
        listing = _listing(
            state, exchanges, limit=limit, cursor=cursor, include_denied=include_denied
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    return {
        "exchanges": [exchange_summary(item) for item in listing.page],
        "returned": len(listing.page),
        "matched": listing.matched,
        "hidden": listing.hidden,
        "limit": listing.limit,
        "next_cursor": str(listing.page[-1].id)
        if listing.page and len(listing.page) < listing.available
        else None,
        "evicted": listing.evicted,
        "recorded": listing.recorded,
        **extra,
    }


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        return int(cursor)
    except ValueError:
        raise ToolError(f"cursor must come from a previous response, got {cursor!r}") from None
