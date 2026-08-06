"""Rendering a Timeline for a model: summary first, and the sentence that explains the failure.

ADR-0004 records the cost of its own decision — interval-shaped output takes more tokens than a
flat list — and answers it with ``verbosity`` plus summary-first rendering. This module is where
that promise is kept.

Two things carry over from :mod:`.views` unchanged, because they are the reasons that module is
shaped as it is. **Nothing here can carry a body**: an :class:`Occurrence` has no field for one, so
a Timeline cannot flood a context window however it is rendered. And **absent is cheaper than
null**: a Step that caused no traffic omits ``owned`` rather than sending an empty list, the same
reasoning that keeps a null ``error`` out of an exchange summary.

The ``explanation`` line is the project's whole thesis rendered in one sentence. An agent should
not have to redo the interval arithmetic to learn that the assertion began while the request it
depended on was still outstanding — that *is* the answer, so the Timeline says it.
"""

from __future__ import annotations

from typing import Any

from https_proxy_mcp.correlate.timeline import Occurrence, StepAttribution, Timeline
from https_proxy_mcp.runs.registry import Run

VERBOSITIES = ("summary", "full")

#: Distinct Background hosts named in a summary. Enough to recognise what the noise is, few enough
#: that a chatty Session does not bury the Steps.
BACKGROUND_HOSTS_SHOWN = 10


def timeline_view(
    timeline: Timeline,
    run: Run,
    *,
    verbosity: str = "summary",
    include_background: bool = False,
    hidden: int = 0,
) -> dict[str, Any]:
    """A Run's Timeline, shaped for a model.

    The two knobs do different jobs. ``verbosity`` controls how much detail each traffic row
    carries; ``include_background`` decides whether Background traffic is *listed* as well as
    counted. Background is never hidden either way — a Timeline that omits a connection reads as
    "there was no traffic", which is the thing this project most wants not to say.

    ``hidden`` is how many Exchanges the narrowing removed. Reported always, because a Timeline
    assembles itself rather than going through ``_page`` and still owes its caller the count every
    listing gives.
    """
    if verbosity not in VERBOSITIES:
        raise ValueError(f"verbosity must be one of {', '.join(VERBOSITIES)}, not {verbosity!r}")

    detailed = verbosity == "full"
    start, end = timeline.window
    return {
        "run": {
            "id": run.id,
            "label": run.display_label,
            "command": list(run.command),
            "started": round(run.t_start, 3),
            "duration_ms": run.duration_ms,
            "exit_code": run.exit_code,
            "passed": run.passed,
            "timed_out": run.timed_out,
            "finished": run.finished,
        },
        "window": {"start": round(start, 3), "end": None if end is None else round(end, 3)},
        "steps": [_step_view(item, detailed) for item in timeline.steps],
        "background": _background_view(
            timeline.background, listed=include_background or detailed, detailed=detailed
        ),
        "pending": [_occurrence_view(item, detailed) for item in timeline.pending],
        "counts": {
            "steps": timeline.step_count,
            "exchanges": timeline.exchange_count,
            "background": len(timeline.background),
            "pending": len(timeline.pending),
            "hidden": hidden,
        },
    }


def _step_view(attribution: StepAttribution, detailed: bool = False) -> dict[str, Any]:
    """One Step and the traffic around it."""
    step = attribution.step
    view: dict[str, Any] = {
        "label": step.label,
        "status": step.status,
        "t_start": round(step.t_start, 3),
        "duration_ms": round(step.duration_ms, 3),
    }
    if step.error is not None:
        view["error"] = step.error
    if attribution.owned:
        view["owned"] = [_occurrence_view(item, detailed) for item in attribution.owned]
    if attribution.overlapping:
        view["overlapping"] = [_occurrence_view(item, detailed) for item in attribution.overlapping]
    if step.failed:
        view["explanation"] = _explain(attribution)
    return view


def _explain(attribution: StepAttribution) -> str:
    """Why this Step's failure looks the way it does, in one factual sentence.

    Ordered by how diagnostic each fact is. A request that was *outstanding* when the Step began is
    the strongest available explanation and the reason ADR-0004 exists; an error status on traffic
    the Step itself caused is next; no traffic at all is a real answer too, and a different one.

    States only what the intervals show. It never says a request *caused* the failure, because the
    Timeline cannot know that — it can only say what was true at the same time.
    """
    step = attribution.step

    outstanding = [item for item in attribution.overlapping if not item.long_lived]
    if outstanding:
        named = ", ".join(_name(item) for item in outstanding[:3])
        return (
            f"{step.label} began while {named} "
            f"{'was' if len(outstanding) == 1 else 'were'} still in flight"
        )

    failed = [item for item in attribution.owned if item.status_code and item.status_code >= 400]
    if failed:
        return f"{step.label} follows {', '.join(_name(item) for item in failed[:3])}"

    if not attribution.owned:
        return f"{step.label} failed and no traffic was recorded during it"

    return (
        f"{step.label} failed after {len(attribution.owned)} request(s) that all completed"
        " successfully"
    )


def _name(occurrence: Occurrence) -> str:
    """How an Occurrence is referred to inside a sentence."""
    identifier = "still open" if occurrence.exchange_id is None else f"#{occurrence.exchange_id}"
    status = f" ({occurrence.status_code})" if occurrence.status_code else ""
    return f"{identifier} {occurrence.label}{status}"


def _background_view(
    background: tuple[Occurrence, ...], *, listed: bool, detailed: bool
) -> dict[str, Any]:
    """Background traffic: always counted and named, listed only when asked for.

    Never omitted. Background is polling, telemetry and prefetching, reported separately rather
    than attributed or discarded (CONTEXT.md) — but a default that lists forty rows of it buries
    the Steps it is meant to frame.
    """
    hosts = list(dict.fromkeys(item.host for item in background))
    view: dict[str, Any] = {
        "count": len(background),
        "hosts": hosts[:BACKGROUND_HOSTS_SHOWN],
    }
    if len(hosts) > BACKGROUND_HOSTS_SHOWN:
        view["hosts_omitted"] = len(hosts) - BACKGROUND_HOSTS_SHOWN
    if listed:
        view["exchanges"] = [_occurrence_view(item, detailed) for item in background]
    return view


def _occurrence_view(occurrence: Occurrence, detailed: bool = False) -> dict[str, Any]:
    """One Exchange or open request as a Timeline row.

    ``id`` is ``None`` for a pending request, which is not an omission: ids are assigned on
    completion, so there genuinely is not one yet.
    """
    view: dict[str, Any] = {
        "id": occurrence.exchange_id,
        "t_start": round(occurrence.t_start, 3),
        "duration_ms": occurrence.duration_ms,
        "method": occurrence.method,
        "host": occurrence.host,
        "path": occurrence.path,
        "status": occurrence.status_code,
    }
    if detailed:
        # Sizes, never bodies. "The response was two megabytes" explains a duration; the bytes
        # themselves stay behind an explicit get_exchange_body call.
        view["request_bytes"] = occurrence.request_bytes
        view["response_bytes"] = occurrence.response_bytes
    # Flags only when true. Two false booleans on every row is a real cost at fifty rows.
    if occurrence.pending:
        view["pending"] = True
    if occurrence.long_lived:
        view["long_lived"] = True
    if occurrence.error is not None:
        view["error"] = occurrence.error
    return view
