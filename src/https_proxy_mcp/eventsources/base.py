"""The Event Source contract: where a Run's Steps come from.

Either a test tool's own artifacts, read after the fact, or explicit marks emitted while the Run
is in progress (CONTEXT.md). Both are peers. The explicit source is the *universal* one — any tool
that can shell out, or a human exploring by hand, can mark Steps with no adapter and no parsing —
and Maestro's parser is the enrichment path rather than the foundation (SPEC.md).

The contract is deliberately one method. Everything a source needs is on the :class:`Run`: its
artifacts directory and its window. Adding a parameter here would be adding it to every future
source, so the Run carries the context instead.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from https_proxy_mcp.correlate.steps import Step
from https_proxy_mcp.runs.registry import Run


@runtime_checkable
class EventSource(Protocol):
    """Produces the Steps of a Run."""

    #: How this source is named in a Timeline, so an agent can tell a parsed Step from a marked
    #: one when the two disagree.
    name: str

    def steps(self, run: Run) -> list[Step]:
        """The Steps of this Run, in any order — the Correlator sorts by time.

        Returns an empty list rather than raising when this source has nothing to say. Every
        source is consulted for every Run, so "not applicable" is the common case, not an error:
        a `curl` command produces no Maestro artifacts and that is entirely normal.
        """
