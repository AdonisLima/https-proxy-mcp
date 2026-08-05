"""Spawning the test command, and owning the flags that decide where its artifacts land.

ADR-0003 has the MCP spawn the test rather than observe one the user started. That buys exact Run
boundaries — the Run *is* the interval from spawn to exit, with no matching heuristic — and it
removes clock skew by construction, since the test tool and the proxy then share one host clock.

It also means this module launches arbitrary commands, which is a capability worth being careful
with. Three precautions are load-bearing rather than tidy:

* **stdin is closed.** The server speaks JSON-RPC over stdin, and a subprocess inheriting it would
  read the client's next message. That is the same permanent hang ``sudo -n`` exists to prevent in
  P3, arriving by a different door.
* **The environment is merged, never replaced.** ``maestro`` is a shell script that needs ``java``
  on ``PATH``; substituting the environment breaks the very tool being wrapped.
* **A timeout kills the process group.** Maestro launches a JVM. Killing only the wrapper leaves
  that holding the simulator and the port, so the *next* Run fails for a reason that looks
  entirely unrelated.

Output goes to files in the Run directory and only a tail comes back, because Maestro's output is
long and returning it whole would flood the context this tool exists to protect.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from dataclasses import dataclass
from pathlib import Path

from https_proxy_mcp.runs.registry import Run

#: Lines of each stream returned to the caller. The files keep everything.
DEFAULT_TAIL_LINES = 40

#: Characters of each stream returned, whatever the line count. A line-count limit alone is no
#: protection: one JSON blob can be megabytes on a single line.
MAX_TAIL_CHARS = 8_000

#: How long a terminated process is given to exit before it is killed outright.
KILL_GRACE_S = 2.0

#: Handed to the spawned command so a tool with no adapter — or a shell script marking Steps —
#: can find the Run's own directory without being passed a flag it may not accept.
RUN_DIR_ENV = "HTTPS_PROXY_MCP_RUN_DIR"

#: Maestro's flags for where output goes. Both are pointed at one directory, which is what
#: structurally eliminates the documented footgun of splitting them (ADR-0003).
#:
#: Worth knowing, from probing Maestro 2.6.1: ``--test-output-dir`` does **not** contain the debug
#: output — its own help says so, and the directory came back empty. ``commands-*.json`` lives
#: under ``--debug-output`` alone. Both are still set, so artifacts stay together.
MAESTRO_OUTPUT_FLAGS = ("--debug-output", "--test-output-dir")

#: Makes the artifact path deterministic. Without it Maestro writes into a hidden
#: ``.maestro/tests/<timestamp>/`` subdirectory.
MAESTRO_FLATTEN_FLAG = "--flatten-debug-output"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """What became of the spawned command."""

    #: ``None`` when the process never ran, or was killed before reporting one.
    exit_code: int | None
    timed_out: bool
    stdout_tail: str
    stderr_tail: str
    #: Why it could not be run at all — a misspelled binary, most often. ``None`` when it ran.
    error: str | None = None


def parse_command(command: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a command into argv.

    A string is split the way a shell would but is **never handed to one**, so nothing in it is
    interpreted — no globbing, no substitution, no chaining. Strings are accepted because an agent
    writes ``"maestro test login.yaml"`` far more naturally than a JSON array.
    """
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        raise ValueError("command is empty; give the test command to run")
    return tuple(argv)


def maestro_flags(argv: tuple[str, ...], run: Run) -> tuple[str, ...]:
    """Point Maestro's output flags at this Run's artifacts directory.

    Recognised by the binary's *name* rather than the whole path, since it is normally invoked
    through ``~/.maestro/bin/maestro``. Only ``maestro test`` takes these flags — ``maestro studio``
    would fail outright if given one.

    A flag the caller already supplied is left exactly as they wrote it. Overriding would be the
    tool second-guessing an explicit instruction, and it would silently move their artifacts.
    """
    if not argv or Path(argv[0]).name != "maestro" or "test" not in argv[1:]:
        return argv

    extra: list[str] = []
    for flag in MAESTRO_OUTPUT_FLAGS:
        if not _has_flag(argv, flag):
            extra += [flag, str(run.artifacts_dir)]
    if not _has_flag(argv, MAESTRO_FLATTEN_FLAG):
        extra.append(MAESTRO_FLATTEN_FLAG)

    return argv + tuple(extra)


async def execute(
    argv: tuple[str, ...],
    *,
    run: Run,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> ProcessOutcome:
    """Run the command to completion, or kill it on its deadline."""
    environment = {**os.environ, RUN_DIR_ENV: str(run.dir), **(env or {})}

    try:
        with run.stdout_path.open("wb") as out, run.stderr_path.open("wb") as err:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdout=out,
                stderr=err,
                # Nothing to read. See the module docstring: inheriting the server's stdin would
                # let the subprocess eat the MCP client's next message.
                stdin=asyncio.subprocess.DEVNULL,
                # Its own process group, so a timeout can take the JVM with it.
                start_new_session=True,
            )
            timed_out = await _wait(process, timeout_ms)
            exit_code = process.returncode
    except (OSError, ValueError) as exc:
        # A misspelled binary is an answer, not a traceback: the agent that typed it is the one
        # who has to fix it.
        return ProcessOutcome(
            exit_code=None,
            timed_out=False,
            stdout_tail="",
            stderr_tail="",
            error=f"could not run {shlex.join(argv)}: {exc}",
        )

    return ProcessOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_tail=_tail(run.stdout_path, tail_lines),
        stderr_tail=_tail(run.stderr_path, tail_lines),
    )


async def _wait(process: asyncio.subprocess.Process, timeout_ms: int | None) -> bool:
    """Wait for the process, killing its group if it overruns. True if it was killed."""
    if timeout_ms is None:
        await process.wait()
        return False

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_ms / 1000)
        return False
    except TimeoutError:
        await _kill_group(process)
        return True


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Terminate the process group, then kill whatever is left.

    The group rather than the process, because the thing worth stopping is usually a child: the
    JVM Maestro launches outlives its wrapper and keeps hold of the simulator.
    """
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_S)
            return
        except TimeoutError:
            continue


def _has_flag(argv: tuple[str, ...], flag: str) -> bool:
    """Whether the caller already gave this flag, in either ``--flag value`` or ``--flag=value``."""
    return any(part == flag or part.startswith(f"{flag}=") for part in argv)


def _tail(path: Path, lines: int) -> str:
    """The end of a stream, bounded by both lines and characters.

    The end rather than the beginning, because a failure message is at the bottom.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    tail = "\n".join(text.splitlines()[-lines:])
    if len(tail) > MAX_TAIL_CHARS:
        kept = tail[-MAX_TAIL_CHARS:]
        return f"<truncated: showing the last {len(kept)} of {len(tail)} characters>\n{kept}"
    return tail
