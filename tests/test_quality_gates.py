"""The gates' own test: what the quality gates promise, read from the manifest.

Every other test in this suite is green *because the gates held*. The gates themselves — mypy's
strictness, pytest's warning policy — are configuration, and configuration drifts silently:
nothing fails when a ``warn_unreachable`` is dropped or an ``error`` filter slides down the list,
because the suite would have to run the tools and ask. This module is the project's single seam
for that question, in the manner of the USAGE.md signature pinning and the ``sudo -n`` assertion
— not by re-running the tools, but by reading the manifest they run from and asserting the
promise it makes.
"""

import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "pyproject.toml"


def gate_configuration() -> dict:
    with MANIFEST.open("rb") as handle:
        return tomllib.load(handle)["tool"]


def test_mypy_flags_unreachable_code():
    """``--strict`` alone leaves dead branches unexamined: code after a ``return``, a condition
    that cannot hold. That is usually the author being wrong about the types, and the suite
    reporting green over code mypy never looked at. The flag is what closes that gap, so its
    absence is a failed promise rather than a style choice."""
    assert gate_configuration()["mypy"]["warn_unreachable"] is True


def test_pytest_fails_on_any_warning_outside_the_one_narrowing():
    """A warning that only reaches a report is a warning nobody reads, so the policy is
    error-first with exceptions argued for individually. The order is the assertion: pytest
    applies filters in sequence and later entries win, so ``error`` must lead and each narrowing
    must follow it — an ``ignore`` above the ``error`` silently swallows everything below it.
    """
    filters = gate_configuration()["pytest"]["ini_options"]["filterwarnings"]

    assert filters[0] == "error"
    # The one standing exception: mitmproxy calls deprecated pyparsing and pyasn1 APIs, and there
    # is nothing to fix on this side of the dependency. Scoped to deprecations so every other
    # category — ours included — still fails the run.
    assert "ignore:.*deprecated.*:DeprecationWarning" in filters[1:]
