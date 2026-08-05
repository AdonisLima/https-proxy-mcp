"""The Step — one action or assertion within a Run.

This is the **entire contract** the Correlator depends on, and nothing in it is Maestro-shaped.
That is what makes the explicit ``start_step``/``end_step`` source and the Maestro artifact parser
peers rather than one being a special case of the other, and it is what lets a Detox or Appium
source arrive later as an addition (SPEC.md, "Adapter contracts").

Times are epoch seconds, matching Exchange timestamps, because the test tool and the proxy run on
the same host — device time never enters the join (ADR-0003). Durations are milliseconds, because
that is the unit every test tool reports and the unit a human reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: What became of a Step.
#:
#: ``unknown`` exists so an Event Source meeting a status it does not recognise can say so.
#: Mapping an unrecognised status onto ``passed`` would be the one unsafe direction: a Timeline
#: claiming a Step succeeded when nobody knows is worse than one admitting ignorance.
StepStatus = Literal["passed", "failed", "skipped", "running", "unknown"]


@dataclass(frozen=True, slots=True)
class Step:
    """One action or assertion, with its own start, duration and outcome (CONTEXT.md)."""

    #: Epoch seconds.
    t_start: float
    duration_ms: float
    label: str
    status: StepStatus
    #: Why it failed, when it did. One line — never a UI hierarchy or a stack trace; see
    #: :mod:`https_proxy_mcp.eventsources.maestro` for why that matters.
    error: str | None = None

    @property
    def t_end(self) -> float:
        """When it finished. A zero-duration Step ends exactly where it started."""
        return self.t_start + self.duration_ms / 1000

    @property
    def failed(self) -> bool:
        return self.status == "failed"
