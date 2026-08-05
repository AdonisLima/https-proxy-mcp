"""Event Source implementations.

Each source produces a Run's Steps — either by parsing a test tool's artifacts
after the fact, or by recording explicit marks emitted while the Run is running.

The two that ship in v1 are peers. Explicit marks are the universal source, needing no adapter at
all; Maestro's parser is the enrichment path (SPEC.md). Every source is consulted for every Run and
returns nothing when it has nothing to say, so no caller has to decide which one applies.
"""

from https_proxy_mcp.eventsources.base import EventSource
from https_proxy_mcp.eventsources.explicit import StepLog, StepStateError
from https_proxy_mcp.eventsources.maestro import MaestroEventSource

__all__ = ["EventSource", "MaestroEventSource", "StepLog", "StepStateError"]
