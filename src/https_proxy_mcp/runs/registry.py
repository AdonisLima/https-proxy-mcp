"""A Run's identity, its directory, and the manifest that outlives the process.

The Run *is* the window. It comes from process lifetime rather than from inference (ADR-0003):
`run_test` spawns the command, and the Run is exactly the interval from spawn to exit. That is
what removes the matching heuristic, the fuzzy edges, and clock skew — the test tool and the proxy
share one host clock, so Step and Exchange timestamps are directly comparable.

Ids are short ordinals (``run-1``) rather than the microsecond timestamps a Session uses. The
reasoning differs because the requirement does: a Session id exists so retention can sort
directories without opening them, while a Run id exists to be **passed back** by an agent calling
``get_timeline``. ``run-2`` is far harder to get wrong than ``20260805T183443-771234``.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

#: Runs live under this subdirectory of the Session directory (SPEC.md).
RUNS_DIRNAME = "runs"

#: The test tool's own output. Maestro's flags are pointed here, which is what structurally
#: eliminates the documented footgun of splitting them (ADR-0003).
ARTIFACTS_DIRNAME = "artifacts"

RUN_MANIFEST = "run.json"

STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of an end-to-end test (CONTEXT.md)."""

    id: str
    dir: Path
    command: tuple[str, ...]
    #: Epoch seconds, from the same clock as every Exchange.
    t_start: float
    #: ``None`` while the process is still running.
    t_end: float | None = None
    #: ``None`` while still running. Maestro exits 1 on a failing flow, 0 on a passing one.
    exit_code: int | None = None
    timed_out: bool = False
    label: str | None = None
    cwd: str | None = None

    @property
    def artifacts_dir(self) -> Path:
        return self.dir / ARTIFACTS_DIRNAME

    @property
    def stdout_path(self) -> Path:
        return self.dir / STDOUT_FILENAME

    @property
    def stderr_path(self) -> Path:
        return self.dir / STDERR_FILENAME

    @property
    def window(self) -> tuple[float, float | None]:
        """The Run as an interval, for :class:`~https_proxy_mcp.capture.reporting.ReportingFilter`.

        An open end is a Run still in progress, and reports everything since it started — which is
        what makes a Timeline answerable mid-flight rather than only after the fact.
        """
        return (self.t_start, self.t_end)

    @property
    def finished(self) -> bool:
        return self.t_end is not None

    @property
    def passed(self) -> bool:
        """Whether the test command reported success.

        A timeout is never a pass, whatever the exit code: a process killed on a deadline can
        return 0 by coincidence, and the deadline is the more truthful signal.
        """
        return self.finished and not self.timed_out and self.exit_code == 0

    @property
    def duration_ms(self) -> float | None:
        if self.t_end is None:
            return None
        return round((self.t_end - self.t_start) * 1000, 3)

    @property
    def display_label(self) -> str:
        """What a Timeline calls this Run. Falls back to the command, because ``run-1`` on its own
        tells an agent nothing about what it ran."""
        return self.label or shlex.join(self.command)

    def to_manifest(self) -> dict[str, object]:
        """The contents of ``run.json``. Not the directory path — that is where it is."""
        return {
            "id": self.id,
            "label": self.label,
            "command": list(self.command),
            "cwd": self.cwd,
            "started": self.t_start,
            "ended": self.t_end,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }


class RunRegistry:
    """The Runs of one Session, and the directories they own."""

    def __init__(self, session_dir: Path | str) -> None:
        self._runs_root = Path(session_dir) / RUNS_DIRNAME
        self._runs: dict[str, Run] = {}

    def create(
        self,
        *,
        command: tuple[str, ...],
        t_start: float,
        label: str | None = None,
        cwd: str | None = None,
    ) -> Run:
        """Allocate the next Run, make its directories, and write the opening manifest.

        The manifest is written **before** the process starts, so a crash between spawn and exit
        leaves a Run that says it never finished rather than no Run at all.
        """
        run_id = f"run-{len(self._runs) + 1}"
        run_dir = self._runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        # mkdir's mode is subject to umask; chmod is not. Test output routinely carries request
        # detail, and Maestro's artifacts embed a whole UI hierarchy.
        self._runs_root.chmod(0o700)
        run_dir.chmod(0o700)
        artifacts = run_dir / ARTIFACTS_DIRNAME
        artifacts.mkdir()
        artifacts.chmod(0o700)

        run = Run(id=run_id, dir=run_dir, command=command, t_start=t_start, label=label, cwd=cwd)
        self._write(run)
        self._runs[run_id] = run
        return run

    def complete(
        self, run: Run, *, t_end: float, exit_code: int | None, timed_out: bool = False
    ) -> Run:
        """Close a Run's window and rewrite its manifest. Returns the completed Run."""
        completed = replace(run, t_end=t_end, exit_code=exit_code, timed_out=timed_out)
        self._write(completed)
        self._runs[completed.id] = completed
        return completed

    def get(self, run_id: str) -> Run | None:
        """The Run with this id, or ``None``. The tool layer turns that into a message naming
        the Runs that do exist."""
        return self._runs.get(run_id)

    def all(self) -> list[Run]:
        """Every Run of this Session, in the order they ran."""
        return list(self._runs.values())

    def latest(self) -> Run | None:
        """The most recent Run, or ``None`` if none has been started."""
        return next(reversed(self._runs.values()), None) if self._runs else None

    def _write(self, run: Run) -> None:
        (run.dir / RUN_MANIFEST).write_text(json.dumps(run.to_manifest(), indent=2))
