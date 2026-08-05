"""Spawning the test command: the Run's window, and the flags that decide where artifacts land.

ADR-0003 makes the MCP spawn the test rather than observe one the user ran, which buys exact Run
boundaries and removes clock skew by construction. The cost is that this module launches arbitrary
commands, so what it does with them is worth pinning down: real subprocesses here, not mocks,
because the failure modes that matter — a timeout leaving an orphaned JVM, a replaced environment
losing PATH — only exist for real ones.

The Maestro flag injection is the other half of ADR-0003: because the MCP builds the command line,
it can point `--debug-output` at the Run's own artifacts directory and structurally eliminate the
documented footgun of splitting Maestro's two output flags.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from https_proxy_mcp.runs.process import (
    DEFAULT_TAIL_LINES,
    execute,
    maestro_flags,
    parse_command,
)
from https_proxy_mcp.runs.registry import RunRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def registry(tmp_path):
    return RunRegistry(tmp_path)


@pytest.fixture
def run(registry):
    return registry.create(command=("python",), t_start=time.time())


def python(script: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script)


# --- the command line ---------------------------------------------------------


def test_a_string_command_is_split_like_a_shell_would():
    """Agents write "maestro test login.yaml" far more naturally than a JSON array, and both
    should work. Split rather than handed to a shell, so nothing is interpreted."""
    assert parse_command("maestro test login.yaml") == ("maestro", "test", "login.yaml")


def test_a_quoted_argument_survives_splitting():
    assert parse_command('maestro test "my flow.yaml"') == ("maestro", "test", "my flow.yaml")


def test_a_list_command_is_taken_as_given():
    assert parse_command(["maestro", "test", "a b.yaml"]) == ("maestro", "test", "a b.yaml")


def test_an_empty_command_is_refused():
    with pytest.raises(ValueError):
        parse_command("   ")


# --- Maestro flag injection ---------------------------------------------------


def test_maestro_gets_its_output_flags_pointed_at_the_runs_artifacts(run):
    """The whole of ADR-0003's third consequence, in one assertion."""
    argv = maestro_flags(("maestro", "test", "login.yaml"), run)

    assert argv[:3] == ("maestro", "test", "login.yaml")
    assert "--debug-output" in argv
    assert argv[argv.index("--debug-output") + 1] == str(run.artifacts_dir)
    assert "--test-output-dir" in argv
    assert argv[argv.index("--test-output-dir") + 1] == str(run.artifacts_dir)


def test_maestro_gets_the_flatten_flag_so_the_artifact_path_is_deterministic(run):
    """Without it the file lands in a hidden `.maestro/tests/<timestamp>/` subdirectory. The
    parser searches recursively anyway, but a deterministic path is worth having."""
    assert "--flatten-debug-output" in maestro_flags(("maestro", "test", "a.yaml"), run)


def test_flags_the_caller_supplied_are_not_overridden(run):
    """A caller who named a directory meant it. Overriding would be the tool second-guessing an
    explicit instruction, and it would silently move their artifacts."""
    argv = maestro_flags(("maestro", "test", "--debug-output", "/tmp/mine", "a.yaml"), run)

    assert argv.count("--debug-output") == 1
    assert argv[argv.index("--debug-output") + 1] == "/tmp/mine"


def test_maestro_is_recognised_by_the_binary_name_not_the_whole_path(run):
    """It is normally invoked through ~/.maestro/bin/maestro."""
    argv = maestro_flags(("/Users/someone/.maestro/bin/maestro", "test", "a.yaml"), run)

    assert "--debug-output" in argv


def test_a_maestro_subcommand_that_is_not_test_is_left_alone(run):
    """`maestro studio` and `maestro download-samples` take neither flag, and adding one would
    make the command fail outright."""
    assert maestro_flags(("maestro", "studio"), run) == ("maestro", "studio")


def test_a_command_that_is_not_maestro_is_left_alone(run):
    assert maestro_flags(("pytest", "-q"), run) == ("pytest", "-q")


# --- running ------------------------------------------------------------------


async def test_a_successful_command_reports_exit_zero(run):
    outcome = await execute(python("print('hi')"), run=run)

    assert outcome.exit_code == 0
    assert outcome.timed_out is False


async def test_a_failing_command_reports_its_exit_code(run):
    """Maestro exits 1 on a failing flow, and that is the Run's headline result."""
    outcome = await execute(python("raise SystemExit(3)"), run=run)

    assert outcome.exit_code == 3


async def test_output_is_written_to_the_run_directory(run):
    await execute(python("import sys; print('out'); print('err', file=sys.stderr)"), run=run)

    assert "out" in run.stdout_path.read_text()
    assert "err" in run.stderr_path.read_text()


async def test_only_a_tail_of_the_output_comes_back(run):
    """Maestro's output is long, and returning it whole would flood the context this tool
    exists to protect. The files hold everything; the return value holds the end of it."""
    outcome = await execute(python(f"print('\\n'.join(str(i) for i in range({500})))"), run=run)

    lines = outcome.stdout_tail.splitlines()
    assert len(lines) <= DEFAULT_TAIL_LINES
    # The end, not the beginning — a failure message is at the bottom.
    assert lines[-1] == "499"
    # Nothing was lost from the file itself.
    assert len(run.stdout_path.read_text().splitlines()) == 500


async def test_a_single_enormous_line_is_capped_too(run):
    """A line-count limit alone is no protection: one JSON blob can be megabytes."""
    outcome = await execute(python("print('x' * 500_000)"), run=run)

    assert len(outcome.stdout_tail) < 20_000


async def test_a_command_that_does_not_exist_is_reported_not_raised(run):
    """An agent typing a wrong binary name should get an answer, not a traceback."""
    outcome = await execute(("definitely-not-a-real-binary-xyz",), run=run)

    assert outcome.exit_code is None
    assert outcome.error is not None
    assert "definitely-not-a-real-binary-xyz" in outcome.error


# --- the environment ----------------------------------------------------------


async def test_extra_environment_is_merged_not_substituted(run):
    """Replacing the environment drops PATH, which breaks the very tool being wrapped —
    `maestro` is a shell script that needs `java` on PATH."""
    outcome = await execute(
        python("import os; print(os.environ['MY_VAR'], bool(os.environ.get('PATH')))"),
        run=run,
        env={"MY_VAR": "set"},
    )

    assert "set True" in outcome.stdout_tail


async def test_the_working_directory_is_honoured(run, tmp_path):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()

    outcome = await execute(python("import os; print(os.getcwd())"), run=run, cwd=str(workdir))

    assert str(workdir.resolve()) in outcome.stdout_tail


# --- timeouts -----------------------------------------------------------------


async def test_a_command_that_overruns_is_killed_and_reported(run):
    started = time.monotonic()

    outcome = await execute(python("import time; time.sleep(30)"), run=run, timeout_ms=400)

    assert outcome.timed_out is True
    # Killed on the deadline rather than waited out.
    assert time.monotonic() - started < 10


async def test_a_timeout_kills_the_whole_process_group(run, tmp_path):
    """Maestro launches a JVM. Killing only the wrapper leaves that running, holding the
    simulator and the port, and the next Run fails for a reason that looks unrelated.
    """
    marker = tmp_path / "child-survived"
    # A parent that spawns a long-lived child, then sleeps. Killing only the parent leaves the
    # child to write the marker.
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(2);"
        f" open({str(marker)!r}, 'w').write('x')\"]); "
        "time.sleep(30)"
    )

    outcome = await execute(python(script), run=run, timeout_ms=400)
    assert outcome.timed_out is True

    # Long enough for the child's sleep to have elapsed had it survived.
    time.sleep(3)
    assert not marker.exists(), "the spawned child outlived the timeout"


async def test_a_command_finishing_inside_its_timeout_is_not_a_timeout(run):
    outcome = await execute(python("print('quick')"), run=run, timeout_ms=10_000)

    assert outcome.timed_out is False
    assert outcome.exit_code == 0


async def test_no_timeout_means_the_command_decides_when_it_is_done(run):
    outcome = await execute(python("print('done')"), run=run, timeout_ms=None)

    assert outcome.exit_code == 0


# --- isolation ---------------------------------------------------------------


async def test_the_spawned_command_is_given_no_stdin(run, monkeypatch):
    """The server speaks JSON-RPC over stdin. A subprocess that reads it would consume the
    client's next message — the same class of permanent hang `sudo -n` exists to prevent in P3.

    Asserted on the spawn arguments rather than on observed behaviour, deliberately. Under pytest
    stdin is already redirected, so a test that reads it from the child passes whether or not the
    precaution is in place — it looked like a test and proved nothing.
    """
    seen: dict[str, object] = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen.update(kwargs)
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    await execute(python("pass"), run=run)

    assert seen["stdin"] is asyncio.subprocess.DEVNULL
    # And its own process group, which is what lets a timeout take a JVM with it.
    assert seen["start_new_session"] is True


async def test_the_run_is_told_where_to_put_artifacts_through_the_environment(run):
    """So a test tool with no adapter — or a shell script marking Steps — can find the Run's own
    directory without being handed a flag it may not accept."""
    script = "import os; print(os.environ['HTTPS_PROXY_MCP_RUN_DIR'])"
    outcome = await execute(python(script), run=run)

    assert str(run.dir) in outcome.stdout_tail
