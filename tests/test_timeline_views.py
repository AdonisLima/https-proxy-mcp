"""Rendering a Timeline for a model: summary first, and the sentence that explains the failure.

ADR-0004 notes the cost of its own decision — interval-shaped output takes more tokens than a flat
list — and answers it with `verbosity` and summary-first rendering. These tests hold that line: the
default must stay compact, must never carry a body, and must still say the one thing the whole
project exists to say.
"""

from __future__ import annotations

import json

import pytest

from https_proxy_mcp.capture.exchange import Exchange, PendingExchange
from https_proxy_mcp.correlate.steps import Step
from https_proxy_mcp.correlate.timeline import build_timeline
from https_proxy_mcp.runs.registry import RunRegistry
from https_proxy_mcp.serialize.timeline_views import timeline_view


@pytest.fixture
def run(tmp_path):
    registry = RunRegistry(tmp_path)
    created = registry.create(command=("maestro", "test", "login.yaml"), t_start=0.0, label="login")
    return registry.complete(created, t_end=5.0, exit_code=1)


def step(start_ms, duration_ms, label, status="passed", error=None):
    return Step(
        t_start=start_ms / 1000,
        duration_ms=duration_ms,
        label=label,
        status=status,
        error=error,
    )


def exch(
    exchange_id,
    start_ms,
    end_ms,
    *,
    host="api.example.com",
    path="/",
    method="GET",
    status_code=200,
    request_size=0,
    response_size=0,
):
    from https_proxy_mcp.capture.exchange import BodyRef

    def body(size):
        return BodyRef(size=size, stored_bytes=size, path="x" if size else None, truncated=False)

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
        request_body=body(request_size),
        response_body=body(response_size),
    )


def the_failing_case():
    """SPEC.md's worked example: a tap, a failing assertion, and a request that straddles both."""
    return (
        [
            step(0, 80, "tapOn: Login"),
            step(80, 4920, "assertVisible: Home", status="failed", error="not visible"),
        ],
        [exch(7, 12, 912, method="POST", path="/auth", status_code=401)],
    )


# --- the summary --------------------------------------------------------------


def test_the_summary_names_the_run_and_its_outcome(run):
    steps, exchanges = the_failing_case()
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    view = timeline_view(timeline, run)

    assert view["run"]["id"] == "run-1"
    assert view["run"]["label"] == "login"
    assert view["run"]["exit_code"] == 1
    assert view["run"]["passed"] is False


def test_each_step_carries_its_outcome_and_what_it_caused(run):
    steps, exchanges = the_failing_case()
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    view = timeline_view(timeline, run)

    tap, assertion = view["steps"]
    assert tap["label"] == "tapOn: Login"
    assert [item["id"] for item in tap["owned"]] == [7]
    assert assertion["status"] == "failed"
    assert assertion["error"] == "not visible"
    # The sentence that explains the failure.
    assert [item["id"] for item in assertion["overlapping"]] == [7]


def test_a_step_that_caused_nothing_says_so_without_empty_lists(run):
    """Fifty rows of `"owned": []` is pure context cost, the same reasoning that keeps a null
    `error` out of an exchange summary."""
    timeline = build_timeline(steps=[step(0, 100, "lonely")], exchanges=[], window=run.window)

    (rendered,) = timeline_view(timeline, run)["steps"]

    assert "owned" not in rendered
    assert "overlapping" not in rendered
    assert "error" not in rendered


def test_the_summary_explains_a_failing_step_in_one_sentence(run):
    """The whole point of the project, rendered. An agent should not have to do the interval
    arithmetic itself to learn that the assertion began while its request was outstanding."""
    steps, exchanges = the_failing_case()
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    view = timeline_view(timeline, run)

    explanation = view["steps"][1]["explanation"]
    assert "still in flight" in explanation
    assert "#7" in explanation
    assert "POST api.example.com/auth" in explanation


def test_a_failing_step_with_no_traffic_at_all_says_that_instead(run):
    """A different and equally useful diagnosis: the screen never asked for anything."""
    steps = [step(0, 100, "assertVisible: Home", status="failed", error="not visible")]
    timeline = build_timeline(steps=steps, exchanges=[], window=run.window)

    explanation = timeline_view(timeline, run)["steps"][0]["explanation"]

    assert "no traffic" in explanation


def test_a_passing_step_gets_no_explanation(run):
    """Nothing to explain, and a sentence per passing Step would double the Timeline."""
    timeline = build_timeline(steps=[step(0, 100, "tapOn: Login")], exchanges=[], window=run.window)

    assert "explanation" not in timeline_view(timeline, run)["steps"][0]


def test_the_explanation_names_a_failing_status_code_when_there_is_one(run):
    """ "The request came back 401" is a better answer than "a request overlapped"."""
    steps = [step(0, 1000, "assertVisible: Home", status="failed", error="not visible")]
    exchanges = [exch(7, 10, 20, method="POST", path="/auth", status_code=401)]
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    explanation = timeline_view(timeline, run)["steps"][0]["explanation"]

    assert "401" in explanation


# --- background and pending ---------------------------------------------------


def test_background_traffic_is_counted_not_listed_in_the_summary(run):
    """Background is polling and telemetry. It must be visible — never dropped — but a summary
    that lists forty rows of it buries the Steps."""
    steps = [step(500, 100, "later")]
    exchanges = [exch(i, 10 + i, 20 + i, host=f"host{i}.example.com") for i in range(1, 6)]
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    view = timeline_view(timeline, run)

    assert view["background"]["count"] == 5
    assert "exchanges" not in view["background"]
    assert len(view["background"]["hosts"]) == 5


def test_background_can_be_listed_without_asking_for_full_detail(run):
    """`include_background` and `verbosity` do different jobs: one decides whether Background is
    listed, the other how much detail each row carries."""
    steps = [step(500, 100, "later")]
    timeline = build_timeline(steps=steps, exchanges=[exch(1, 10, 20)], window=run.window)

    view = timeline_view(timeline, run, include_background=True)

    assert [item["id"] for item in view["background"]["exchanges"]] == [1]
    # Still the compact row: no sizes.
    assert "response_bytes" not in view["background"]["exchanges"][0]


def test_full_detail_adds_body_sizes_but_never_bodies(run):
    """A size explains a duration. The bytes stay behind an explicit get_exchange_body call."""
    steps, _ = the_failing_case()
    exchanges = [exch(7, 12, 912, request_size=120, response_size=2048)]
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    row = timeline_view(timeline, run, verbosity="full")["steps"][0]["owned"][0]

    assert row["request_bytes"] == 120
    assert row["response_bytes"] == 2048


def test_full_verbosity_lists_the_background_exchanges(run):
    steps = [step(500, 100, "later")]
    exchanges = [exch(1, 10, 20), exch(2, 30, 40)]
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    view = timeline_view(timeline, run, verbosity="full")

    assert [item["id"] for item in view["background"]["exchanges"]] == [1, 2]


def test_a_request_still_in_flight_is_reported_as_pending(run):
    still_open = PendingExchange(
        key="k",
        t_start=0.045,
        method="POST",
        url="https://api.example.com/graphql",
        host="api.example.com",
        path="/graphql",
        scheme="https",
    )
    timeline = build_timeline(
        steps=[step(0, 5000, "waiting")], exchanges=[], pending=[still_open], window=run.window
    )

    view = timeline_view(timeline, run)

    assert view["pending"][0]["pending"] is True
    assert view["pending"][0]["id"] is None
    assert view["counts"]["pending"] == 1


def test_a_long_lived_connection_is_flagged_where_it_is_listed(run):
    timeline = build_timeline(
        steps=[step(0, 100, "one"), step(100, 100, "two")],
        exchanges=[exch(1, 10, 4000, path="/ws", status_code=101)],
        window=run.window,
    )

    view = timeline_view(timeline, run)

    assert view["steps"][0]["owned"][0]["long_lived"] is True
    # Listed once, so the second Step has nothing at all.
    assert "overlapping" not in view["steps"][1]


# --- the structural guards ---------------------------------------------------


def test_no_rendering_carries_a_body_at_any_verbosity(run):
    """The same guard `list_exchanges` has, enforced on the Timeline. An agent must not be able
    to flood its context by asking for a Timeline."""
    steps, exchanges = the_failing_case()
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    for verbosity in ("summary", "full"):
        rendered = json.dumps(timeline_view(timeline, run, verbosity=verbosity))
        assert "body" not in rendered
        assert "preview" not in rendered


def test_the_summary_is_materially_smaller_than_the_full_view(run):
    """Otherwise `verbosity` is decoration and ADR-0004's cost is unmitigated."""
    steps = [step(500, 100, "later")]
    exchanges = [exch(i, 10 + i, 20 + i, host=f"host{i}.example.com") for i in range(1, 30)]
    timeline = build_timeline(steps=steps, exchanges=exchanges, window=run.window)

    summary = len(json.dumps(timeline_view(timeline, run)))
    full = len(json.dumps(timeline_view(timeline, run, verbosity="full")))

    assert summary * 2 < full


def test_an_unknown_verbosity_is_refused_rather_than_guessed(run):
    timeline = build_timeline(steps=[], exchanges=[], window=run.window)

    with pytest.raises(ValueError):
        timeline_view(timeline, run, verbosity="everything")


def test_the_hidden_count_is_reported(run):
    """A Timeline does its own assembly rather than going through `_page`, so it owes its caller
    the same `hidden` count every listing gives — or dropped rows read as "there was no traffic"."""
    timeline = build_timeline(steps=[], exchanges=[], window=run.window)

    view = timeline_view(timeline, run, hidden=4)

    assert view["counts"]["hidden"] == 4


def test_the_window_is_reported_so_the_selection_can_be_checked(run):
    timeline = build_timeline(steps=[], exchanges=[], window=run.window)

    view = timeline_view(timeline, run)

    assert view["window"] == {"start": 0.0, "end": 5.0}
