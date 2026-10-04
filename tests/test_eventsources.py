"""Event Sources: where a Run's Steps come from.

Two ship in v1 and they are peers, not a base case and a special case. Explicit
``start_step``/``end_step`` is the universal source — any tool that can shell out, or a human
exploring by hand, can mark Steps with no artifact parsing at all. Maestro's parser is the
*enrichment* path (SPEC.md).

The Maestro tests run against `tests/fixtures/maestro-commands-failing-assert.json`, which is the
genuine 123 KB artifact a probe captured from Maestro 2.6.1 driving a real simulator — hierarchy,
stack trace and all. Two of its properties would have produced a wrong or unusable Timeline, and
both are asserted here rather than trusted:

* the array is **not in execution order** (`[seq 3, 0, 1, 2]`), and
* one failing command carries **33 KB of UI hierarchy** inside `error`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from https_proxy_mcp.correlate.steps import Step
from https_proxy_mcp.eventsources import ARTIFACT_SOURCES, EventSource
from https_proxy_mcp.eventsources.explicit import StepLog, StepStateError
from https_proxy_mcp.eventsources.maestro import MaestroEventSource
from https_proxy_mcp.runs.registry import RunRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "maestro-commands-failing-assert.json"


@pytest.fixture
def run(tmp_path):
    """A Run whose window is wide enough to hold the fixture's timestamps."""
    registry = RunRegistry(tmp_path)
    created = registry.create(command=("maestro", "test", "flow.yaml"), t_start=0.0)
    return registry.complete(created, t_end=2_000_000_000.0, exit_code=1)


def write_fixture(run, name: str = "commands-(flow.yaml).json") -> None:
    (run.artifacts_dir / name).write_bytes(FIXTURE.read_bytes())


# --- the Maestro parser, against real output ---------------------------------


def test_the_fixture_really_is_out_of_order(run):
    """Guards the guard. If a future Maestro emits ordered arrays this test fails, and the
    ordering assertions below stop proving anything — better to be told than to drift."""
    entries = json.loads(FIXTURE.read_text())

    assert [entry["metadata"]["sequenceNumber"] for entry in entries] == [3, 0, 1, 2]


def test_steps_are_ordered_by_sequence_number_not_by_array_position(run):
    """The finding that would have shipped a wrong Timeline. The FAILED assertion is *first* in
    the file and ran *last*; ordering by position puts the failure before the launch."""
    write_fixture(run)

    steps = MaestroEventSource().steps(run)

    assert [step.label for step in steps] == [
        "launchApp: com.apple.mobilesafari",
        'assertCondition: "ThisTextIsDefinitelyNotOnScreen"',
    ]


def test_the_failing_step_carries_its_message_and_nothing_else(run):
    """33 KB of UI hierarchy per failure is the context blowout PLAN.md names as a risk. The
    parser drops it at the boundary, so no downstream code can leak it by accident."""
    write_fixture(run)

    steps = MaestroEventSource().steps(run)

    failing = steps[-1]
    assert failing.status == "failed"
    assert failing.error == 'Assertion is false: "ThisTextIsDefinitelyNotOnScreen" is visible'
    # The whole point: the hierarchy, the stack trace and the debug text are gone.
    assert "hierarchyRoot" not in (failing.error or "")
    assert len(failing.error or "") < 200


def test_no_step_carries_more_than_a_line_of_error(run):
    """Structural rather than per-case, so a new Maestro error field cannot slip through."""
    write_fixture(run)

    for step in MaestroEventSource().steps(run):
        assert step.error is None or len(step.error) < 500


def test_timestamps_are_converted_from_milliseconds_to_epoch_seconds(run):
    """Maestro reports epoch **ms**; every Exchange timestamp is epoch seconds. Getting this
    wrong puts every Step roughly fifty thousand years from the traffic it should align with."""
    write_fixture(run)

    steps = MaestroEventSource().steps(run)

    # 1785965701868 ms, the launch command's start.
    assert steps[0].t_start == pytest.approx(1785965701.868)
    assert steps[0].duration_ms == 663


def test_maestro_bookkeeping_commands_are_not_steps(run):
    """`defineVariables` and `applyConfiguration` are Maestro's own setup, not an action or an
    assertion (CONTEXT.md), and they are never the cause of a failure. Filtered by an explicit
    list of keys, so an unrecognised command is kept rather than silently dropped."""
    write_fixture(run)

    labels = [step.label for step in MaestroEventSource().steps(run)]

    assert not any("defineVariables" in label for label in labels)
    assert not any("applyConfiguration" in label for label in labels)


def test_a_completed_command_is_a_passed_step(run):
    write_fixture(run)

    assert MaestroEventSource().steps(run)[0].status == "passed"


def test_an_unrecognised_status_becomes_unknown_rather_than_passed(run, tmp_path):
    """The one unsafe direction. A Timeline claiming a Step succeeded when nobody knows is
    worse than one admitting ignorance."""
    (run.artifacts_dir / "commands-(x.yaml).json").write_text(
        json.dumps(
            [
                {
                    "command": {"tapOnElementCommand": {"selector": {"textRegex": "Login"}}},
                    "metadata": {
                        "status": "SOMETHING_NEW",
                        "timestamp": 1785965701868,
                        "duration": 10,
                        "sequenceNumber": 0,
                    },
                }
            ]
        )
    )

    assert MaestroEventSource().steps(run)[0].status == "unknown"


# --- finding the artifact -----------------------------------------------------


def test_the_parser_finds_the_artifact_in_a_nested_debug_directory(run):
    """Without `--flatten-debug-output` Maestro writes to a **hidden** `.maestro/tests/<stamp>/`.
    `run_test` injects the flatten flag, but a caller supplying their own flags must still work."""
    nested = run.artifacts_dir / ".maestro" / "tests" / "2026-08-05_183653"
    nested.mkdir(parents=True)
    (nested / "commands-(failing.yaml).json").write_bytes(FIXTURE.read_bytes())

    assert len(MaestroEventSource().steps(run)) == 2


def test_no_artifacts_at_all_means_no_steps_rather_than_an_error(run):
    """Both sources are consulted for every Run, so a non-Maestro command must be silent here
    rather than raising."""
    assert MaestroEventSource().steps(run) == []


def test_a_corrupt_artifact_is_skipped_rather_than_ending_the_timeline(run):
    """A half-written file is what a killed Maestro leaves behind, and that is exactly when a
    Timeline is most wanted. One unreadable file must not take the Run's other Steps with it."""
    write_fixture(run)
    (run.artifacts_dir / "commands-(truncated.yaml).json").write_text('[{"command": {')

    assert len(MaestroEventSource().steps(run)) == 2


def test_steps_from_several_flows_are_merged_in_time_order(run):
    """`sequenceNumber` restarts per flow, so it cannot order across files — time can."""
    (run.artifacts_dir / "commands-(second.yaml).json").write_text(
        json.dumps(
            [
                {
                    "command": {"tapOnElementCommand": {"selector": {"textRegex": "Later"}}},
                    "metadata": {
                        "status": "COMPLETED",
                        # After everything in the main fixture.
                        "timestamp": 1785965799999,
                        "duration": 5,
                        "sequenceNumber": 0,
                    },
                }
            ]
        )
    )
    write_fixture(run)

    steps = MaestroEventSource().steps(run)

    assert steps[-1].label == 'tapOnElement: "Later"'


# --- explicit Steps -----------------------------------------------------------


def test_a_marked_step_becomes_a_step(run):
    log = StepLog()

    log.start("tapOn: Login", t_start=100.0)
    log.end(status="passed", t_end=100.08)

    (step,) = log.steps(run)
    assert step.label == "tapOn: Login"
    assert step.status == "passed"
    assert step.duration_ms == pytest.approx(80, abs=0.001)


def test_a_failing_marked_step_carries_its_error(run):
    log = StepLog()
    log.start("assertVisible: Home", t_start=100.0)

    log.end(status="failed", error="never appeared", t_end=105.0)

    (step,) = log.steps(run)
    assert step.status == "failed"
    assert step.error == "never appeared"


def test_starting_a_step_while_one_is_open_is_an_error(run):
    """Not an implicit close. A Step's status is the entire point of recording one, and closing
    the previous Step silently would record an outcome nobody stated."""
    log = StepLog()
    log.start("first", t_start=100.0)

    with pytest.raises(StepStateError) as raised:
        log.start("second", t_start=101.0)

    # The message has to name the way out, because an agent reads it.
    assert "end_step" in str(raised.value)
    assert "first" in str(raised.value)


def test_ending_with_no_step_open_is_an_error(run):
    log = StepLog()

    with pytest.raises(StepStateError) as raised:
        log.end(status="passed", t_end=100.0)

    assert "start_step" in str(raised.value)


def test_an_open_step_appears_as_running(run):
    """A Timeline asked for mid-Step should show the Step that is happening, not omit it."""
    log = StepLog()
    log.start("in progress", t_start=100.0)

    (step,) = log.steps(run, now=100.5)

    assert step.status == "running"
    assert step.duration_ms == pytest.approx(500, abs=0.001)


def test_closing_a_step_replaces_the_running_one(run):
    log = StepLog()
    log.start("thing", t_start=100.0)
    log.end(status="passed", t_end=101.0)

    steps = log.steps(run, now=200.0)

    assert len(steps) == 1
    assert steps[0].status == "passed"


def test_steps_outside_the_runs_window_are_not_that_runs_steps(run, tmp_path):
    """Explicit Steps live in the Session, not in a Run, so a Run has to select its own —
    otherwise marks from an earlier Run would pollute a later Timeline."""
    registry = RunRegistry(tmp_path / "other")
    early = registry.complete(
        registry.create(command=("x",), t_start=10.0), t_end=20.0, exit_code=0
    )

    log = StepLog()
    log.start("during the early run", t_start=12.0)
    log.end(status="passed", t_end=13.0)
    log.start("long after it", t_start=900.0)
    log.end(status="passed", t_end=901.0)

    assert [step.label for step in log.steps(early)] == ["during the early run"]


def test_marks_are_ordered_by_start(run):
    log = StepLog()
    for index, start in enumerate([300.0, 100.0, 200.0]):
        log.start(f"step-{index}", t_start=start)
        log.end(status="passed", t_end=start + 1)

    assert [step.t_start for step in log.steps(run)] == [100.0, 200.0, 300.0]


# --- the shared contract ------------------------------------------------------


@pytest.mark.parametrize("source", [*ARTIFACT_SOURCES, StepLog()], ids=lambda source: source.name)
def test_every_event_source_satisfies_the_protocol(source, run):
    """PLAN.md asks for one shared suite every Event Source must pass. This is what keeps the
    core honest as sources multiply, and what makes Detox later an addition rather than a change:
    a source added to ``ARTIFACT_SOURCES`` is covered here with no further edit.
    """
    assert isinstance(source, EventSource)
    assert source.name

    steps = source.steps(run)

    assert isinstance(steps, list)
    assert all(isinstance(step, Step) for step in steps)
    # Nothing Maestro-shaped may leak through the contract.
    allowed = {"passed", "failed", "skipped", "running", "unknown"}
    assert all(step.status in allowed for step in steps)
