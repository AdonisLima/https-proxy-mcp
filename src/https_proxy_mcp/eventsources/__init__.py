"""Event Source implementations.

Each source produces a Run's Steps — either by parsing a test tool's artifacts
after the fact, or by recording explicit marks emitted while the Run is running.

The two that ship in v1 are peers. Explicit marks are the universal source, needing no adapter at
all; Maestro's parser is the enrichment path (SPEC.md). Every source is consulted for every Run and
returns nothing when it has nothing to say, so no caller has to decide which one applies.

Artifact-parsing sources are consulted through the ``ARTIFACT_SOURCES`` registry below — a new
parser (Detox, Appium, Playwright, XCUITest) is an addition in this one place, and is then
automatically consulted by every Timeline and automatically covered by the shared contract suite.
"""

from https_proxy_mcp.eventsources.base import EventSource
from https_proxy_mcp.eventsources.explicit import StepLog, StepStateError
from https_proxy_mcp.eventsources.maestro import MaestroEventSource

#: Every artifact-parsing Event Source, consulted for every Run. Module-level instances are safe
#: because a parser is stateless — everything it needs is on the Run it is handed.
#:
#: ``StepLog`` is deliberately **not** here: it holds Session-scoped mutable state and is created
#: per Session in ``CaptureState``, so a module-level ``StepLog`` would leak explicit marks across
#: Sessions. It joins the iteration at the one consumption site instead.
ARTIFACT_SOURCES: tuple[EventSource, ...] = (MaestroEventSource(),)

__all__ = [
    "ARTIFACT_SOURCES",
    "EventSource",
    "MaestroEventSource",
    "StepLog",
    "StepStateError",
]
