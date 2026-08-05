"""Runs: one execution of an end-to-end test, as a named window inside a Session.

Two concerns, kept apart because only one of them touches the outside world. :mod:`.registry`
holds a Run's identity, its directory and its manifest — the same job ``capture/session.py`` does
one level up. :mod:`.process` spawns the test command and owns the flags that decide where its
artifacts land.

Neither knows anything about correlation. :mod:`https_proxy_mcp.correlate` is pure arithmetic over
whatever Steps an Event Source produced, and it stays that way.
"""

from https_proxy_mcp.runs.registry import Run, RunRegistry

__all__ = ["Run", "RunRegistry"]
