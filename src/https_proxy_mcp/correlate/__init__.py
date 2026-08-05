"""Correlation layer: Steps and Exchanges into a Timeline.

Implements owner-plus-overlap attribution (see docs/adr/0004-overlap-attribution.md):
an Exchange is owned by the Step active when it started, and every later Step it
overlaps is recorded. Depends only on the Step contract, never on a test tool.

Pure by design — nothing here touches a disk, a process or mitmproxy, which is what lets the
arithmetic be exercised with small integers instead of a running proxy.
"""

from https_proxy_mcp.correlate.steps import Step, StepStatus
from https_proxy_mcp.correlate.timeline import (
    Occurrence,
    StepAttribution,
    Timeline,
    build_timeline,
)

__all__ = [
    "Occurrence",
    "Step",
    "StepAttribution",
    "StepStatus",
    "Timeline",
    "build_timeline",
]
