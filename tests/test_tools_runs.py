"""The Run and Timeline tools, driven through the real tool surface.

`get_timeline` is the one tool in the project that assembles its own result instead of going
through `_page`, so it does not inherit the denylist and the Scope for free. Several tests below
exist only to prove it applies them anyway — that is the failure HANDOFF.md warned about.

The proxy is stubbed: these tests are about the tool layer, and the capture path already has its
own end-to-end coverage. Traffic is put into the store directly.
"""

from __future__ import annotations

import json
import time

import pytest

from https_proxy_mcp.capture.exchange import CapturedExchange, PendingExchange
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from stub_runtime import StubRuntime
from tool_calls import call


@pytest.fixture
def state(tmp_path):
    return CaptureState(tmp_path, runtime_factory=StubRuntime)


@pytest.fixture
def server(state):
    return build_server(state=state)


@pytest.fixture
async def recording(server, state):
    await call(server, "start_capture", target="curl", port=0)
    yield server
    if state.recording:
        await call(server, "stop_capture")


def record(state, *, t_start, t_end, host="api.example.com", path="/", method="GET", status=200):
    """Put one completed Exchange into the store, as the addon would."""
    return state.store.record(
        CapturedExchange(
            t_start=t_start,
            t_end=t_end,
            method=method,
            url=f"https://{host}{path}",
            host=host,
            path=path,
            scheme="https",
            http_version="HTTP/1.1",
            status_code=status,
        )
    )


def open_request(state, *, key, t_start, host="api.example.com", path="/graphql"):
    state.store.open(
        PendingExchange(
            key=key,
            t_start=t_start,
            method="POST",
            url=f"https://{host}{path}",
            host=host,
            path=path,
            scheme="https",
        )
    )


async def mark(server, label, *, status="passed", error=None):
    await call(server, "start_step", label=label)
    return await call(server, "end_step", status=status, error=error)


# --- run_test -----------------------------------------------------------------


async def test_running_a_command_creates_a_run_and_reports_its_outcome(recording):
    result = await call(recording, "run_test", command="python3 -c 'raise SystemExit(1)'")

    assert result["run"] == "run-1"
    assert result["exit_code"] == 1
    assert result["passed"] is False
    assert "get_timeline" in result["next"]


async def test_a_failing_test_is_a_result_not_an_error(recording):
    """A failing test is the normal reason to reach for this tool. Raising would make the common
    case an exception and force an agent to catch it to read the answer."""
    result = await call(recording, "run_test", command="python3 -c 'raise SystemExit(3)'")

    assert result["exit_code"] == 3


async def test_a_label_names_the_run(recording):
    result = await call(recording, "run_test", command="python3 -c ''", label="login flow")

    assert result["label"] == "login flow"


async def test_run_test_needs_a_recording_session(server):
    """Runs live inside a Session (CONTEXT.md), and a Run with no capture answers nothing."""
    with pytest.raises(Exception, match="not recording"):
        await call(server, "run_test", command="python3 -c ''")


async def test_a_command_that_cannot_run_is_reported_in_the_result(recording):
    result = await call(recording, "run_test", command="definitely-not-a-binary-xyz")

    assert result["error"] is not None
    assert result["exit_code"] is None


async def test_an_empty_command_is_refused(recording):
    with pytest.raises(Exception, match="command is empty"):
        await call(recording, "run_test", command="   ")


async def test_runs_are_listed_with_their_outcomes(recording):
    await call(recording, "run_test", command="python3 -c ''", label="first")
    await call(recording, "run_test", command="python3 -c 'raise SystemExit(1)'", label="second")

    listed = await call(recording, "list_runs")

    assert listed["count"] == 2
    assert [item["label"] for item in listed["runs"]] == ["first", "second"]
    assert [item["passed"] for item in listed["runs"]] == [True, False]


# --- explicit Steps -----------------------------------------------------------


async def test_marking_a_step_records_it(recording):
    result = await mark(recording, "tapOn: Login")

    assert result["label"] == "tapOn: Login"
    assert result["status"] == "passed"


async def test_starting_two_steps_without_ending_the_first_is_refused(recording):
    await call(recording, "start_step", label="first")

    with pytest.raises(Exception, match="already open"):
        await call(recording, "start_step", label="second")


async def test_ending_with_nothing_open_is_refused(recording):
    with pytest.raises(Exception, match="no Step is open"):
        await call(recording, "end_step")


async def test_an_invalid_status_is_refused_rather_than_recorded(recording):
    """`running` is not an ending, and `unknown` is for a parser that met something it did not
    recognise — not for an agent that could simply say."""
    await call(recording, "start_step", label="thing")

    with pytest.raises(Exception, match="status must be one of"):
        await call(recording, "end_step", status="running")


async def test_an_open_step_is_surfaced_by_get_status(recording):
    """A Step left open shows as running in every later Timeline, and nothing else would say why."""
    await call(recording, "start_step", label="forgotten")

    assert (await call(recording, "get_status"))["open_step"] == "forgotten"


# --- get_timeline: the acceptance shape ---------------------------------------


async def test_the_timeline_shows_a_failing_step_and_what_was_in_flight(recording, state):
    """P4's acceptance criterion, at the tool boundary: the failing assertion *and* the Exchange
    still in flight when it began.

    No `run_test` here, because that tool blocks — an agent driving the app itself marks Steps and
    reads a Session-wide Timeline, which is exactly what the universal Event Source is for.
    """
    await call(recording, "start_step", label="tapOn: Login")
    # Fired *during* the tap, and still going long after — so it is owned by the tap and was
    # outstanding when the assertion began. Recorded between the marks so the intervals really
    # interleave rather than being fabricated around a single instant.
    now = time.time()
    record(state, t_start=now, t_end=now + 10, method="POST", path="/auth", status=401)
    await call(recording, "end_step")
    await call(recording, "start_step", label="assertVisible: Home")
    await call(recording, "end_step", status="failed", error="not visible")

    timeline = await call(recording, "get_timeline")

    assertion = next(item for item in timeline["steps"] if item["status"] == "failed")
    assert assertion["label"] == "assertVisible: Home"
    assert "still in flight" in assertion["explanation"]
    assert "401" in assertion["explanation"]


async def test_an_empty_timeline_says_when_the_session_has_traffic_outside_the_window(
    recording, state
):
    """`run_test` blocks, so Steps marked afterwards fall outside its window — correctly, but
    invisibly. An agent that marked Steps after running a test would otherwise get an empty
    Timeline with nothing to explain it.
    """
    await call(recording, "run_test", command="python3 -c ''")
    # Marked after the Run finished, so outside its window.
    record(state, t_start=time.time(), t_end=time.time() + 0.1)
    await mark(recording, "after the run")

    timeline = await call(recording, "get_timeline")

    assert timeline["counts"]["steps"] == 0
    assert "note" in timeline
    assert "outside" in timeline["note"]


async def test_a_request_still_open_appears_as_pending_with_no_id(recording, state):
    now = time.time()
    open_request(state, key="k", t_start=now)
    await mark(recording, "waiting", status="failed", error="timed out")

    timeline = await call(recording, "get_timeline")

    assert timeline["pending"][0]["id"] is None
    assert timeline["pending"][0]["pending"] is True


async def test_a_timeline_can_be_had_with_no_run_at_all(recording, state):
    """Marking Steps by hand must work without `run_test`, or the universal Event Source is not
    universal."""
    record(state, t_start=time.time(), t_end=time.time() + 0.1)
    await mark(recording, "did a thing by hand")

    timeline = await call(recording, "get_timeline")

    assert timeline["run"]["id"] == "session"
    assert [item["label"] for item in timeline["steps"]] == ["did a thing by hand"]


async def test_an_unknown_run_id_names_the_runs_that_do_exist(recording):
    await call(recording, "run_test", command="python3 -c ''")

    with pytest.raises(Exception, match="run-1"):
        await call(recording, "get_timeline", run_id="run-99")


async def test_an_unknown_verbosity_is_refused(recording):
    with pytest.raises(Exception, match="verbosity must be one of"):
        await call(recording, "get_timeline", verbosity="everything")


# --- get_timeline: narrowing it must not forget -------------------------------


async def test_the_timeline_applies_the_builtin_denylist(recording, state):
    """`get_timeline` assembles its own result, so it does not inherit the denylist from `_page`.
    HANDOFF.md flagged exactly this as the thing to get wrong."""
    now = time.time()
    record(state, t_start=now, t_end=now + 0.1, host="gs-loc.apple.com")
    record(state, t_start=now, t_end=now + 0.1, host="api.example.com")
    await mark(recording, "a step")

    timeline = await call(recording, "get_timeline")

    assert timeline["counts"]["hidden"] == 1
    assert "gs-loc.apple.com" not in json.dumps(timeline)


async def test_the_timeline_applies_the_session_scope(server, state):
    await call(server, "start_capture", target="curl", port=0, scope=["api.example.com"])
    try:
        now = time.time()
        record(state, t_start=now, t_end=now + 0.1, host="other.example.org")
        record(state, t_start=now, t_end=now + 0.1, host="api.example.com")
        await mark(server, "a step")

        timeline = await call(server, "get_timeline")

        assert timeline["counts"]["hidden"] == 1
        assert "other.example.org" not in json.dumps(timeline)
    finally:
        await call(server, "stop_capture")


async def test_the_run_window_excludes_traffic_from_before_the_run(recording, state):
    """The first of SPEC.md's three narrowings. Traffic from an earlier Run cannot pollute this
    Timeline."""
    long_ago = time.time() - 3600
    record(state, t_start=long_ago, t_end=long_ago + 0.1, path="/old")

    await call(recording, "run_test", command="python3 -c ''")
    timeline = await call(recording, "get_timeline")

    assert "/old" not in json.dumps(timeline)
    assert timeline["counts"]["hidden"] == 1


async def test_a_host_filter_narrows_further_without_widening_the_scope(server, state):
    """A host filter is a narrowing, never a replacement. A Scope the agent set on the Session
    still holds — asking about one host inside it must not reveal traffic outside it."""
    await call(server, "start_capture", target="curl", port=0, scope=["api.example.com"])
    try:
        now = time.time()
        record(state, t_start=now, t_end=now + 0.1, host="api.example.com")
        record(state, t_start=now, t_end=now + 0.1, host="cdn.example.org")
        await mark(server, "a step")

        outside = await call(server, "get_timeline", host_filter="cdn.example.org")

        assert "cdn.example.org" not in json.dumps(outside)
        assert outside["counts"]["exchanges"] == 0
    finally:
        await call(server, "stop_capture")


async def test_a_host_filter_covers_subdomains_like_any_other_host_entry(recording, state):
    """A host filter is a host entry, so it has to mean what one means everywhere else: itself and
    everything under it.

    This is the second caller of the shared host-set rule — the Scope is the first — and the one
    whose behaviour would go unnoticed if that rule were quietly replaced here by an equality test.
    """
    now = time.time()
    record(state, t_start=now, t_end=now + 0.1, host="api.example.com")
    record(state, t_start=now, t_end=now + 0.1, host="cdn.example.org")
    await mark(recording, "a step")

    timeline = await call(recording, "get_timeline", host_filter="example.com")

    assert timeline["counts"]["exchanges"] == 1
    assert "cdn.example.org" not in json.dumps(timeline)


async def test_background_is_counted_by_default_and_listed_on_request(recording, state):
    now = time.time()
    record(state, t_start=now, t_end=now + 0.1, path="/telemetry")
    # A Step that starts later, so the traffic above belongs to no Step.
    await call(recording, "start_step", label="later")
    await call(recording, "end_step")

    default = await call(recording, "get_timeline")
    listed = await call(recording, "get_timeline", include_background=True)

    assert default["background"]["count"] == 1
    assert "exchanges" not in default["background"]
    assert [item["path"] for item in listed["background"]["exchanges"]] == ["/telemetry"]


async def test_no_timeline_can_return_a_body(recording, state):
    """The structural guard, at the tool boundary this time."""
    now = time.time()
    record(state, t_start=now, t_end=now + 0.1)
    await mark(recording, "a step")

    for verbosity in ("summary", "full"):
        rendered = json.dumps(await call(recording, "get_timeline", verbosity=verbosity))
        assert "preview" not in rendered


# --- export_har ---------------------------------------------------------------


async def test_exporting_writes_a_har_and_returns_only_a_path_and_a_count(recording, state):
    now = time.time()
    record(state, t_start=now, t_end=now + 0.1)

    result = await call(recording, "export_har")

    assert result["entries"] == 1
    assert result["redacted"] is True
    exported = json.loads((state.session.dir / "exports" / "session.har").read_text())
    assert exported["log"]["version"] == "1.2"
    assert "log" not in result


async def test_an_export_defaults_to_the_session_exports_directory(recording, state):
    record(state, t_start=time.time(), t_end=time.time() + 0.1)

    result = await call(recording, "export_har")

    assert result["path"].startswith(str(state.session.dir))


async def test_an_export_can_be_written_where_the_caller_asks(recording, state, tmp_path):
    record(state, t_start=time.time(), t_end=time.time() + 0.1)
    target = tmp_path / "elsewhere" / "out.har"

    result = await call(recording, "export_har", path=str(target))

    assert result["path"] == str(target)
    assert target.exists()


async def test_an_export_is_narrowed_by_the_run_window(recording, state):
    """A Run-scoped export holds that Run's traffic; a Session-wide one holds everything.

    Timestamps are derived from the Run's real window rather than from `time.time()` around it.
    The window narrows by *intersection*, so an Exchange merely recorded "before" the Run still
    counts if it was in flight when the Run began — which is correct, and makes any test that
    guesses at the boundary quietly racy.
    """
    await call(recording, "run_test", command="python3 -c 'import time; time.sleep(0.3)'")
    (details,) = (await call(recording, "list_runs"))["runs"]
    started, ended = details["started"], details["started"] + details["duration_ms"] / 1000

    record(state, t_start=started + 0.05, t_end=started + 0.1, path="/during")
    record(state, t_start=ended + 1, t_end=ended + 2, path="/after")

    scoped = await call(recording, "export_har", run_id="run-1")
    whole = await call(recording, "export_har", path=str(state.session.dir / "all.har"))

    assert scoped["entries"] == 1
    assert whole["entries"] == 2


async def test_stopping_a_session_reports_how_many_runs_it_held(recording, state):
    await call(recording, "run_test", command="python3 -c ''")

    stopped = await call(recording, "stop_capture")

    assert stopped["runs"] == 1
