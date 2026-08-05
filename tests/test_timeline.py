"""The Correlator: owner-plus-overlap arithmetic, and the cases where it goes wrong.

ADR-0004 chose overlap attribution over one-Step-per-Exchange because the flat model destroys
the most diagnostic fact available — *the assertion started while the request it depended on was
still in flight*. These tests are mostly about the boundaries, because that is where an interval
model fails: a Step of zero duration, an Exchange that never completed, an Exchange landing in
the gap between two Steps, and a stream that overlaps everything.

Every helper here takes **milliseconds from an arbitrary zero** and converts, because `[0 ── 80]`
is readable in a way `[1785965701.861 ── 1785965701.941]` is not. The dataclasses themselves hold
epoch seconds, matching Exchange timestamps.
"""

from __future__ import annotations

from https_proxy_mcp.capture.exchange import Exchange, PendingExchange
from https_proxy_mcp.correlate.steps import Step
from https_proxy_mcp.correlate.timeline import Occurrence, build_timeline


def step(start_ms: float, duration_ms: float, label: str, status: str = "passed", **kw) -> Step:
    return Step(
        t_start=start_ms / 1000, duration_ms=duration_ms, label=label, status=status, **kw
    )


def exch(
    exchange_id: int,
    start_ms: float,
    end_ms: float | None,
    *,
    host: str = "api.example.com",
    path: str = "/",
    method: str = "GET",
    status_code: int | None = 200,
    response_headers: list[tuple[str, str]] | None = None,
) -> Exchange:
    return Exchange(
        id=exchange_id,
        t_start=start_ms / 1000,
        t_end=None if end_ms is None else end_ms / 1000,
        method=method,
        url=f"https://{host}{path}",
        host=host,
        path=path,
        scheme="https",
        http_version="HTTP/1.1",
        status_code=status_code,
        response_headers=response_headers or [],
    )


def window(start_ms: float, end_ms: float | None) -> tuple[float, float | None]:
    return (start_ms / 1000, None if end_ms is None else end_ms / 1000)


# --- the worked example from SPEC.md ------------------------------------------


def test_the_spec_worked_example_reproduces_exactly():
    """SPEC.md's own example, which is also P4's acceptance criterion in miniature.

        Step  1  tapOn: Login          [0 ────── 80]
        Step  2  assertVisible: Home            [80 ─────────────────── 5000] FAILED
        Exch  A  POST /auth            [12 ──────────── 912]

    Exchange A is owned by Step 1 and overlaps Step 2. Both halves matter: the ownership says
    the tap caused the request, and the overlap says the assertion began while it was still in
    flight. A flat model reports one and loses the other.
    """
    steps = [
        step(0, 80, "tapOn: Login"),
        step(80, 4920, "assertVisible: Home", status="failed", error="not visible"),
    ]
    auth = exch(1, 12, 912, method="POST", path="/auth")

    timeline = build_timeline(steps=steps, exchanges=[auth], window=window(0, 5000))

    tap, assertion = timeline.steps
    assert [item.exchange_id for item in tap.owned] == [1]
    assert [item.exchange_id for item in tap.overlapping] == []
    # The sentence that explains the failure.
    assert [item.exchange_id for item in assertion.owned] == []
    assert [item.exchange_id for item in assertion.overlapping] == [1]
    assert assertion.step.status == "failed"


# --- ownership ----------------------------------------------------------------


def test_an_exchange_is_owned_by_the_step_that_was_active_when_it_started():
    steps = [step(0, 100, "one"), step(100, 100, "two")]

    timeline = build_timeline(steps=steps, exchanges=[exch(1, 150, 160)], window=window(0, 200))

    assert [item.exchange_id for item in timeline.steps[0].owned] == []
    assert [item.exchange_id for item in timeline.steps[1].owned] == [1]


def test_an_exchange_starting_in_the_gap_between_steps_belongs_to_the_one_that_just_finished():
    """Maestro's inter-command overhead is milliseconds wide, and a tap's request routinely
    lands just after the tap's own metadata window closed.

    Dropping it to Background would lose the Timeline's primary sentence — "the tap made this
    request" — for the most common shape of traffic there is.
    """
    steps = [step(0, 50, "tapOn: Login"), step(100, 100, "assertVisible: Home")]
    # Starts at 60ms: after the tap ended, before the assertion began.
    landed_in_the_gap = exch(1, 60, 70)

    timeline = build_timeline(steps=steps, exchanges=[landed_in_the_gap], window=window(0, 200))

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]
    assert timeline.background == ()


def test_a_zero_duration_step_can_still_own_an_exchange_starting_at_that_instant():
    """A point interval is where off-by-one-interval bugs live, so it is asserted rather than
    assumed. Maestro records 2ms and 4ms commands; a 0ms one is not far-fetched."""
    steps = [step(100, 0, "instant")]

    timeline = build_timeline(steps=steps, exchanges=[exch(1, 100, 200)], window=window(0, 300))

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]


def test_an_exchange_before_the_first_step_is_background_not_attributed():
    """Reported separately rather than attributed or discarded (CONTEXT.md)."""
    steps = [step(100, 100, "one")]

    timeline = build_timeline(steps=steps, exchanges=[exch(1, 10, 20)], window=window(0, 200))

    assert [item.exchange_id for item in timeline.background] == [1]
    assert [item.exchange_id for item in timeline.steps[0].owned] == []


def test_a_step_with_no_exchanges_at_all_still_appears():
    """PLAN.md names this as a golden case. A Step that made no requests is a real answer —
    "the screen never asked for anything" — and a Timeline that omitted it would hide that."""
    timeline = build_timeline(steps=[step(0, 100, "lonely")], exchanges=[], window=window(0, 100))

    assert len(timeline.steps) == 1
    assert timeline.steps[0].owned == ()
    assert timeline.steps[0].overlapping == ()


def test_background_only_run_reports_every_exchange_and_no_steps():
    """The other golden case: traffic with no Steps to attribute it to."""
    timeline = build_timeline(
        steps=[], exchanges=[exch(1, 10, 20), exch(2, 30, 40)], window=window(0, 100)
    )

    assert timeline.steps == ()
    assert [item.exchange_id for item in timeline.background] == [1, 2]


# --- overlap ------------------------------------------------------------------


def test_every_later_step_an_exchange_overlaps_is_recorded():
    steps = [step(0, 100, "one"), step(100, 100, "two"), step(200, 100, "three")]
    spans_all_three = exch(1, 50, 250)

    timeline = build_timeline(steps=steps, exchanges=[spans_all_three], window=window(0, 300))

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]
    assert [item.exchange_id for item in timeline.steps[1].overlapping] == [1]
    assert [item.exchange_id for item in timeline.steps[2].overlapping] == [1]


def test_the_owning_step_does_not_also_list_its_exchange_as_an_overlap():
    """Owned and overlapping are disjoint, or every Timeline row double-counts."""
    timeline = build_timeline(
        steps=[step(0, 100, "one")], exchanges=[exch(1, 10, 20)], window=window(0, 100)
    )

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]
    assert timeline.steps[0].overlapping == ()


def test_an_exchange_that_ended_before_a_step_began_does_not_overlap_it():
    steps = [step(0, 100, "one"), step(100, 100, "two")]

    timeline = build_timeline(steps=steps, exchanges=[exch(1, 10, 20)], window=window(0, 200))

    assert timeline.steps[1].overlapping == ()


# --- exchanges that never completed -------------------------------------------


def test_an_exchange_that_never_completed_overlaps_every_later_step():
    """`t_end is None` comes from the error hook and means the request got no response.

    Extending it to the end of the window is what "never came back" means: it really was
    outstanding for every Step that followed.
    """
    steps = [step(0, 100, "one"), step(100, 100, "two"), step(200, 100, "three")]
    never_returned = exch(1, 50, None, status_code=None)

    timeline = build_timeline(steps=steps, exchanges=[never_returned], window=window(0, 300))

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]
    assert [item.exchange_id for item in timeline.steps[1].overlapping] == [1]
    assert [item.exchange_id for item in timeline.steps[2].overlapping] == [1]


def test_a_pending_exchange_is_placed_like_one_that_never_completed():
    """P4's acceptance criterion in its hardest form: the request is *still open* now, so it
    was certainly in flight when the failing assertion began."""
    steps = [step(0, 80, "tapOn: Login"), step(80, 4920, "assertVisible: Home", status="failed")]
    still_open = PendingExchange(
        key="abc",
        t_start=0.045,
        method="POST",
        url="https://api.example.com/graphql",
        host="api.example.com",
        path="/graphql",
        scheme="https",
    )

    timeline = build_timeline(
        steps=steps, exchanges=[], pending=[still_open], window=window(0, 5000)
    )

    tap, assertion = timeline.steps
    assert [item.label for item in tap.owned] == ["POST api.example.com/graphql"]
    assert [item.pending for item in assertion.overlapping] == [True]
    # No id, because none has been assigned — ids are handed out on completion.
    assert assertion.overlapping[0].exchange_id is None


# --- long-lived connections ---------------------------------------------------


def test_an_upgraded_connection_is_owned_once_and_never_repeated_as_an_overlap():
    """Open question 2. A WebSocket overlaps every Step by nature, so without this rule one
    connection fills every row of the Timeline with itself."""
    steps = [step(0, 100, "one"), step(100, 100, "two"), step(200, 100, "three")]
    websocket = exch(1, 10, 290, path="/ws", status_code=101)

    timeline = build_timeline(steps=steps, exchanges=[websocket], window=window(0, 300))

    assert [item.exchange_id for item in timeline.steps[0].owned] == [1]
    assert timeline.steps[0].owned[0].long_lived is True
    # Listed once, under the Step that opened it, and nowhere else.
    assert timeline.steps[1].overlapping == ()
    assert timeline.steps[2].overlapping == ()


def test_a_server_sent_event_stream_is_long_lived_too():
    """SSE is a 200 with a streaming content type rather than an upgrade, so status alone
    would miss it."""
    steps = [step(0, 100, "one"), step(100, 100, "two")]
    stream = exch(
        1,
        10,
        190,
        path="/events",
        response_headers=[("Content-Type", "text/event-stream; charset=utf-8")],
    )

    timeline = build_timeline(steps=steps, exchanges=[stream], window=window(0, 200))

    assert timeline.steps[0].owned[0].long_lived is True
    assert timeline.steps[1].overlapping == ()


def test_an_ordinary_exchange_is_not_long_lived():
    """The guard against the rule firing on everything."""
    timeline = build_timeline(
        steps=[step(0, 100, "one")], exchanges=[exch(1, 10, 20)], window=window(0, 100)
    )

    assert timeline.steps[0].owned[0].long_lived is False


# --- ordering and shape -------------------------------------------------------


def test_steps_come_back_in_time_order_whatever_order_they_arrived_in():
    """Maestro's artifact is genuinely out of order — the probe read [seq 3, 0, 1, 2] — so the
    Correlator must not inherit its caller's ordering."""
    steps = [step(200, 100, "third"), step(0, 100, "first"), step(100, 100, "second")]

    timeline = build_timeline(steps=steps, exchanges=[], window=window(0, 300))

    assert [item.step.label for item in timeline.steps] == ["first", "second", "third"]


def test_exchanges_within_a_step_are_ordered_by_start_not_by_id():
    """Ids are assigned on completion, so a slow request that started first carries a higher
    id. A Timeline is a time view, so it orders by time."""
    steps = [step(0, 1000, "one")]
    slow_started_first = exch(2, 10, 900)
    fast_started_later = exch(1, 20, 30)

    timeline = build_timeline(
        steps=steps, exchanges=[fast_started_later, slow_started_first], window=window(0, 1000)
    )

    assert [item.exchange_id for item in timeline.steps[0].owned] == [2, 1]


def test_an_occurrence_carries_what_a_timeline_row_needs_and_no_body():
    """The same structural guard as `exchange_summary`: a Timeline cannot carry a body,
    because there is no field to put one in."""
    timeline = build_timeline(
        steps=[step(0, 100, "one")],
        exchanges=[exch(1, 10, 20, method="POST", path="/auth", status_code=401)],
        window=window(0, 100),
    )

    occurrence = timeline.steps[0].owned[0]
    assert occurrence.method == "POST"
    assert occurrence.host == "api.example.com"
    assert occurrence.path == "/auth"
    assert occurrence.status_code == 401
    assert occurrence.duration_ms == 10
    assert not any("body" in field for field in Occurrence.__dataclass_fields__)


def test_the_timeline_reports_its_window_and_its_counts():
    timeline = build_timeline(
        steps=[step(0, 100, "one")],
        exchanges=[exch(1, 10, 20), exch(2, 5000, 5100)],
        window=window(0, 100),
    )

    assert timeline.window == window(0, 100)
    assert timeline.exchange_count == 2
    assert timeline.step_count == 1
