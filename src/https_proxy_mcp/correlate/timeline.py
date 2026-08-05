"""The Correlator: placing Exchanges against Steps by interval overlap.

**Does no I/O.** It takes Steps, Exchanges and a window, and returns a Timeline. No filesystem,
no subprocess, no mitmproxy — the same split that keeps :mod:`https_proxy_mcp.serialize.views`
testable alone, and the reason the arithmetic below can be exercised with small integers instead
of a running proxy.

The model is ADR-0004's: an Exchange is *owned* by the Step that was active when it started —
that Step caused it — and every other Step it overlaps is recorded as an Overlap. The flat
alternative, one Step per Exchange, destroys the most diagnostic fact available: *the assertion
started while the request it depended on was still in flight.*

Three rules are less obvious than the headline, and each exists because the obvious version is
wrong:

* **Ownership is gap-tolerant.** Maestro's inter-command overhead is milliseconds wide, and a
  tap's request routinely starts just after the tap's own metadata window closed. So an Exchange
  is owned by the last Step that had *started* before it, not only by one whose interval strictly
  contains it. Requiring containment would drop "the tap made this request" — the Timeline's
  primary sentence — into Background for the most common shape of traffic there is.
* **An Exchange with no end extends to the end of the window.** That is what "never came back"
  means, and it is why such an Exchange overlaps every Step that followed it.
* **A long-lived connection is listed once and never repeated.** A WebSocket overlaps every Step
  by nature, so without this a single connection fills every row of the Timeline with itself.

Callers pass Exchanges **already narrowed** — the Run window, the denylist and the Scope are
applied by :class:`~https_proxy_mcp.capture.reporting.ReportingFilter` before anything gets here.
This module places what it is given and does not second-guess the selection.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from https_proxy_mcp.capture.exchange import Exchange, PendingExchange
from https_proxy_mcp.correlate.steps import Step

#: HTTP status for a protocol upgrade — the WebSocket handshake, in practice.
SWITCHING_PROTOCOLS = 101

#: A response content type that streams indefinitely. Server-sent events are a ``200``, so status
#: alone would miss them.
STREAMING_CONTENT_TYPE = "text/event-stream"


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One thing with an interval, as a Timeline row sees it.

    Carries no body and no headers, for the same structural reason
    :func:`~https_proxy_mcp.serialize.views.exchange_summary` carries none: there is no field to
    put one in, so a Timeline cannot flood a context window however it is rendered.

    Covers both a recorded Exchange and a still-open request, because the arithmetic treats them
    identically — an interval is an interval. ``exchange_id`` is ``None`` for a pending one, since
    ids are handed out on completion.
    """

    t_start: float
    #: ``None`` for an Exchange that never completed, and for one still in flight.
    t_end: float | None
    method: str
    host: str
    path: str
    exchange_id: int | None = None
    status_code: int | None = None
    pending: bool = False
    long_lived: bool = False
    error: str | None = None
    #: Body *sizes*, not bodies. "The response was two megabytes" is a timing explanation; the
    #: bytes themselves are a separate, explicit call.
    request_bytes: int = 0
    response_bytes: int = 0

    @property
    def label(self) -> str:
        return f"{self.method} {self.host}{self.path}"

    @property
    def duration_ms(self) -> float | None:
        """``None`` when it never finished, which is a different answer from zero."""
        if self.t_end is None:
            return None
        return round((self.t_end - self.t_start) * 1000, 3)

    def effective_end(self, window_end: float | None) -> float:
        """When it stopped mattering, for the purpose of intersecting Steps.

        An Exchange with no end was outstanding for everything that followed, so it runs to the
        end of the window — or forever, in a Session still recording.
        """
        if self.t_end is not None:
            return self.t_end
        return math.inf if window_end is None else window_end

    @classmethod
    def of_exchange(cls, exchange: Exchange) -> Occurrence:
        return cls(
            t_start=exchange.t_start,
            t_end=exchange.t_end,
            method=exchange.method,
            host=exchange.host,
            path=exchange.path,
            exchange_id=exchange.id,
            status_code=exchange.status_code,
            pending=False,
            long_lived=_is_long_lived(exchange),
            error=exchange.error,
            request_bytes=exchange.request_body.size,
            response_bytes=exchange.response_body.size,
        )

    @classmethod
    def of_pending(cls, request: PendingExchange) -> Occurrence:
        return cls(
            t_start=request.t_start,
            t_end=None,
            method=request.method,
            host=request.host,
            path=request.path,
            exchange_id=None,
            status_code=None,
            pending=True,
            # Unknowable while the response is outstanding: an upgrade is only visible in the 101
            # that has not arrived. Claiming it either way here would be a guess.
            long_lived=False,
        )


@dataclass(frozen=True, slots=True)
class StepAttribution:
    """One Step, with the traffic it caused and the traffic that was already in flight."""

    step: Step
    #: Started while this Step was the active one. This Step caused them.
    owned: tuple[Occurrence, ...]
    #: Already in flight when this Step ran, or still running when it ended. The overlap that
    #: usually explains a failure.
    overlapping: tuple[Occurrence, ...]


@dataclass(frozen=True, slots=True)
class Timeline:
    """A Run's Steps interleaved with the Exchanges that happened during it.

    A view over a Session, never a stored thing in its own right (CONTEXT.md).
    """

    window: tuple[float, float | None]
    steps: tuple[StepAttribution, ...]
    #: Inside the window but attributable to no Step — polling, telemetry, prefetching. Reported
    #: separately rather than attributed or discarded.
    background: tuple[Occurrence, ...]
    #: Requests still open. Also placed against Steps above; collected here so a renderer can say
    #: "two requests never came back" without walking the whole Timeline.
    pending: tuple[Occurrence, ...]
    exchange_count: int
    step_count: int


def build_timeline(
    *,
    steps: list[Step],
    exchanges: list[Exchange],
    window: tuple[float, float | None],
    pending: list[PendingExchange] | tuple[PendingExchange, ...] = (),
) -> Timeline:
    """Place Exchanges and open requests against Steps, by ownership and overlap."""
    ordered = sorted(steps, key=lambda item: item.t_start)
    starts = [item.t_start for item in ordered]

    occurrences = sorted(
        [Occurrence.of_exchange(item) for item in exchanges]
        + [Occurrence.of_pending(item) for item in pending],
        key=lambda item: item.t_start,
    )

    owned: list[list[Occurrence]] = [[] for _ in ordered]
    overlapping: list[list[Occurrence]] = [[] for _ in ordered]
    background: list[Occurrence] = []

    window_end = window[1]
    for occurrence in occurrences:
        owner = _owner_index(starts, occurrence.t_start)
        if owner is None:
            background.append(occurrence)
        else:
            owned[owner].append(occurrence)

        if occurrence.long_lived:
            # Listed once, under the Step that opened it. Repeating a stream on every subsequent
            # row is the degenerate case open question 2 asked about.
            continue

        end = occurrence.effective_end(window_end)
        for index, step in enumerate(ordered):
            if index == owner:
                continue
            if occurrence.t_start < step.t_end and step.t_start < end:
                overlapping[index].append(occurrence)

    return Timeline(
        window=window,
        steps=tuple(
            StepAttribution(step=step, owned=tuple(owned[i]), overlapping=tuple(overlapping[i]))
            for i, step in enumerate(ordered)
        ),
        background=tuple(background),
        pending=tuple(item for item in occurrences if item.pending),
        exchange_count=len(exchanges),
        step_count=len(ordered),
    )


def _owner_index(starts: list[float], t_start: float) -> int | None:
    """Which Step was most recently active when something started at ``t_start``.

    ``bisect_right`` finds the first Step that started *after* the given instant, so the one
    before it is the last to have started at or before it. ``None`` means nothing had started
    yet, which makes the Exchange Background.
    """
    index = bisect_right(starts, t_start) - 1
    return index if index >= 0 else None


def _is_long_lived(exchange: Exchange) -> bool:
    """Whether this is a connection that stays open rather than a request/response pair.

    Keyed on what the protocol actually says rather than on a duration threshold, so there is no
    tunable to get wrong and no short WebSocket that slips through.
    """
    if exchange.status_code == SWITCHING_PROTOCOLS:
        return True
    return any(
        name.lower() == "content-type" and STREAMING_CONTENT_TYPE in value.lower()
        for name, value in exchange.response_headers
    )
