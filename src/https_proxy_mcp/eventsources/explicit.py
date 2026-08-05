"""Explicit Steps: the universal Event Source.

``start_step`` / ``end_step`` need no artifact parsing and no adapter, so any test tool that can
shell out — Detox, Appium, XCUITest, a shell script, a human exploring by hand — can produce a
Timeline today. That is why SPEC.md calls this the foundation and Maestro's parser the enrichment.

Marks belong to the **Session**, not to a Run. A human marking Steps has no Run at all, and a Run
selects the marks that fall inside its own window. Storing them per-Run would make the ad-hoc case
impossible and would need a Run to exist before the first mark.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from https_proxy_mcp.correlate.steps import Step, StepStatus
from https_proxy_mcp.runs.registry import Run


class StepStateError(RuntimeError):
    """A mark that does not make sense in the current state.

    Its message names the way out, because an agent is what reads it.
    """


@dataclass(slots=True)
class _OpenStep:
    label: str
    t_start: float


@dataclass(slots=True)
class StepLog:
    """The Steps an agent marked during this Session, and the one currently open."""

    name: str = "explicit"
    _closed: list[Step] = field(default_factory=list)
    _open: _OpenStep | None = None

    @property
    def open_label(self) -> str | None:
        """The Step currently being marked, if any. Reported by ``get_status``."""
        return self._open.label if self._open is not None else None

    def start(self, label: str, *, t_start: float | None = None) -> None:
        """Open a Step.

        Refuses when one is already open rather than closing it implicitly. A Step's status is the
        entire reason for recording one, and an implicit close would have to invent an outcome
        nobody stated — writing "passed" over a Step that may well have failed.
        """
        if self._open is not None:
            raise StepStateError(
                f"a Step is already open ({self._open.label!r}); call end_step before"
                " starting another"
            )
        self._open = _OpenStep(label=label, t_start=time.time() if t_start is None else t_start)

    def end(
        self,
        *,
        status: StepStatus = "passed",
        error: str | None = None,
        t_end: float | None = None,
    ) -> Step:
        """Close the open Step and record it."""
        if self._open is None:
            raise StepStateError("no Step is open; call start_step first")

        ended = time.time() if t_end is None else t_end
        step = Step(
            t_start=self._open.t_start,
            duration_ms=round((ended - self._open.t_start) * 1000, 3),
            label=self._open.label,
            status=status,
            error=error,
        )
        self._closed.append(step)
        self._open = None
        return step

    def steps(self, run: Run, *, now: float | None = None) -> list[Step]:
        """The marked Steps that fall inside this Run's window, in time order.

        An open Step is included as ``running``, measured to ``now``. A Timeline asked for
        mid-Step should show the Step that is happening rather than omit it — which is the whole
        point of being able to ask while a Run is still going.
        """
        candidates = list(self._closed)
        if self._open is not None:
            current = time.time() if now is None else now
            candidates.append(
                Step(
                    t_start=self._open.t_start,
                    duration_ms=round((current - self._open.t_start) * 1000, 3),
                    label=self._open.label,
                    status="running",
                )
            )

        selected = [step for step in candidates if _intersects(step, run)]
        return sorted(selected, key=lambda step: step.t_start)


def _intersects(step: Step, run: Run) -> bool:
    """Whether a Step happened during this Run.

    By intersection, matching how :class:`~https_proxy_mcp.capture.reporting.ReportingFilter`
    treats the window, so a Step and the traffic it caused are never selected on different rules.
    """
    start, end = run.window
    if end is not None and step.t_start > end:
        return False
    return step.t_end >= start
