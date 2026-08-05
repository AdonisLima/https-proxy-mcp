"""The Run: one execution of an end-to-end test, as a named window inside a Session.

A Run is to a Session what `capture/session.py` already models one level up — identity, a
directory, and a lifetime. The window is the whole point: it is the first of SPEC.md's three
narrowings, and it comes from process lifetime rather than inference (ADR-0003), so there is no
matching heuristic and no fuzzy edge.
"""

from __future__ import annotations

import json

import pytest

from https_proxy_mcp.runs.registry import RUN_MANIFEST, RunRegistry


@pytest.fixture
def registry(tmp_path):
    return RunRegistry(tmp_path)


# --- identity -----------------------------------------------------------------


def test_runs_are_numbered_in_order_within_a_session(registry):
    """Short and ordinal rather than a timestamp, because an agent has to pass one back to
    `get_timeline` and `run-2` is easier to get right than `20260805T183443-771234`."""
    first = registry.create(command=("maestro", "test", "a.yaml"), t_start=100.0)
    second = registry.create(command=("maestro", "test", "b.yaml"), t_start=200.0)

    assert (first.id, second.id) == ("run-1", "run-2")


def test_a_run_records_the_command_that_produced_it(registry):
    run = registry.create(command=("maestro", "test", "login.yaml"), t_start=100.0, label="login")

    assert run.command == ("maestro", "test", "login.yaml")
    assert run.label == "login"


def test_a_run_without_a_label_falls_back_to_its_command(registry):
    """A Timeline names its Run, and "run-1" alone tells an agent nothing about what it ran."""
    run = registry.create(command=("maestro", "test", "login.yaml"), t_start=100.0)

    assert run.display_label == "maestro test login.yaml"


# --- the window ---------------------------------------------------------------


def test_a_running_run_has_an_open_ended_window(registry):
    """Exactly what a Run in progress means, and what lets a Timeline be asked for mid-flight."""
    run = registry.create(command=("sleep", "1"), t_start=100.0)

    assert run.window == (100.0, None)
    assert run.finished is False


def test_a_completed_run_has_a_closed_window(registry):
    run = registry.create(command=("sleep", "1"), t_start=100.0)

    completed = registry.complete(run, t_end=105.0, exit_code=0)

    assert completed.window == (100.0, 105.0)
    assert completed.finished is True
    assert completed.duration_ms == 5000


def test_a_completed_run_replaces_the_one_the_registry_holds(registry):
    run = registry.create(command=("sleep", "1"), t_start=100.0)

    registry.complete(run, t_end=105.0, exit_code=3)

    assert registry.get("run-1").exit_code == 3
    assert [item.exit_code for item in registry.all()] == [3]


# --- outcome ------------------------------------------------------------------


def test_a_zero_exit_code_is_a_pass_and_anything_else_is_not(registry):
    """Maestro exits 1 on a failing flow and 0 on a passing one, confirmed by probe."""
    passed = registry.complete(registry.create(command=("x",), t_start=1.0), t_end=2.0, exit_code=0)
    failed = registry.complete(registry.create(command=("y",), t_start=1.0), t_end=2.0, exit_code=1)

    assert passed.passed is True
    assert failed.passed is False


def test_a_run_that_timed_out_is_not_a_pass_whatever_its_exit_code(registry):
    """A killed process can exit 0 by coincidence; the timeout is the more truthful signal."""
    run = registry.create(command=("sleep", "600"), t_start=1.0)

    timed_out = registry.complete(run, t_end=2.0, exit_code=0, timed_out=True)

    assert timed_out.timed_out is True
    assert timed_out.passed is False


def test_an_unfinished_run_has_not_passed(registry):
    run = registry.create(command=("sleep", "1"), t_start=100.0)

    assert run.passed is False


# --- the directory ------------------------------------------------------------


def test_a_run_gets_its_own_directory_with_an_artifacts_subdirectory(registry, tmp_path):
    """SPEC.md's layout. Maestro's output flags are pointed at `artifacts/`, which is what
    structurally removes the footgun of splitting them."""
    run = registry.create(command=("maestro", "test", "a.yaml"), t_start=100.0)

    assert run.dir == tmp_path / "runs" / "run-1"
    assert run.artifacts_dir == run.dir / "artifacts"
    assert run.artifacts_dir.is_dir()


def test_the_run_directory_is_private(registry):
    """Test output routinely holds request and response detail, and Maestro's own artifacts
    embed a full UI hierarchy. Same reasoning as the Session directory being 0700."""
    run = registry.create(command=("maestro",), t_start=100.0)

    assert run.dir.stat().st_mode & 0o777 == 0o700
    assert run.artifacts_dir.stat().st_mode & 0o777 == 0o700


def test_creating_a_run_writes_a_manifest(registry):
    run = registry.create(command=("maestro", "test"), t_start=100.0, label="login")

    manifest = json.loads((run.dir / RUN_MANIFEST).read_text())
    assert manifest["id"] == "run-1"
    assert manifest["label"] == "login"
    assert manifest["command"] == ["maestro", "test"]
    assert manifest["started"] == 100.0
    # Not finished, so there is nothing truthful to write here yet.
    assert manifest["ended"] is None
    assert manifest["exit_code"] is None


def test_completing_a_run_rewrites_the_manifest(registry):
    """The manifest is the durable record. A crash between spawn and exit should leave a Run
    that says it never finished, rather than no Run at all."""
    run = registry.create(command=("maestro",), t_start=100.0)

    completed = registry.complete(run, t_end=105.0, exit_code=1)

    manifest = json.loads((completed.dir / RUN_MANIFEST).read_text())
    assert manifest["ended"] == 105.0
    assert manifest["exit_code"] == 1
    assert manifest["timed_out"] is False


def test_stdout_and_stderr_have_reserved_paths(registry):
    """Kept out of the tool's return value on purpose: Maestro's output is long, and returning
    it would flood the context the tool exists to protect."""
    run = registry.create(command=("maestro",), t_start=100.0)

    assert run.stdout_path == run.dir / "stdout.log"
    assert run.stderr_path == run.dir / "stderr.log"


# --- lookup -------------------------------------------------------------------


def test_an_unknown_run_id_is_none_rather_than_an_error(registry):
    """The tool layer turns this into a message naming the Runs that do exist."""
    assert registry.get("run-99") is None


def test_runs_come_back_in_the_order_they_ran(registry):
    registry.create(command=("a",), t_start=100.0)
    registry.create(command=("b",), t_start=200.0)

    assert [item.id for item in registry.all()] == ["run-1", "run-2"]


def test_the_latest_run_is_the_most_recent_one(registry):
    """What `get_timeline` uses when no run_id is given but Runs exist."""
    registry.create(command=("a",), t_start=100.0)
    second = registry.create(command=("b",), t_start=200.0)

    assert registry.latest().id == second.id


def test_the_latest_run_of_an_empty_registry_is_none(registry):
    assert registry.latest() is None
