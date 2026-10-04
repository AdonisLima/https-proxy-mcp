"""The gates' own test: what the quality gates promise, read from the manifest and the workflow.

Every other test in this suite is green *because the gates held*. The gates themselves — mypy's
strictness, pytest's warning policy, CI's dependency audit — are configuration, and configuration
drifts silently: nothing fails when a ``warn_unreachable`` is dropped or an ``error`` filter
slides down the list, because the suite would have to run the tools and ask. This module is the
project's single seam for that question, in the manner of the USAGE.md signature pinning and the
``sudo -n`` assertion — not by re-running the tools, but by reading the manifest and the CI
workflow they run from and asserting the promise each makes.
"""

import tomllib
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parent.parent / "pyproject.toml"
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def gate_configuration() -> dict:
    with MANIFEST.open("rb") as handle:
        return tomllib.load(handle)["tool"]


def workflow_jobs() -> dict:
    """The CI workflow, read as data. ``yaml`` reaches the suite as a transitive dependency of
    pre-commit; parsing the workflow rather than matching its text means a reformat that keeps
    the promise (a step renamed, a key reordered) does not fail the assertion."""
    with WORKFLOW.open() as handle:
        return yaml.safe_load(handle)["jobs"]


def audit_step() -> dict:
    """The single step, anywhere in the workflow, that audits the dependencies."""
    steps = [
        step
        for job in workflow_jobs().values()
        for step in job["steps"]
        if "run" in step and ("pip-audit" in step["run"] or "uv audit" in step["run"])
    ]
    assert len(steps) == 1
    return steps[0]


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


def test_workflow_audits_the_locked_dependencies():
    """No other gate asks whether the locked resolution carries known vulnerabilities, and the
    manifest itself declares that resolution load-bearing — a wheel ships no lock, so the
    ceilings in pyproject.toml are all a consumer sees. The audit is what watches that surface;
    a workflow that silently dropped the step would be green over unwatched dependencies.
    Asserted by shape here and exercised by CI on push, per the spec's testing decisions."""
    step = audit_step()
    job = next(job for job in workflow_jobs().values() if step in job["steps"])

    # A lock-aware audit: pip-audit reads the environment `uv sync --locked` materialised, so
    # the step is meaningless without the locked sync beside it; `uv audit` reads the lock
    # directly. Either way the audited set is exactly what the lock resolved.
    assert any("run" in s and "uv sync --locked" in s["run"] for s in job["steps"])
    assert "audit" in step["run"]


def test_dependency_audit_runs_on_linux_not_on_the_ten_x_macos_job():
    """The audit needs nothing from macOS, and this private repository bills macOS runners at
    10x against a 2,000-minute monthly allowance. An audit step that drifted onto the test job
    would keep passing while quietly spending ten times what it must."""
    step = audit_step()
    job_name, job = next(
        (name, job) for name, job in workflow_jobs().items() if step in job["steps"]
    )

    assert "ubuntu" in job["runs-on"], f"audit lives on {job_name} ({job['runs-on']})"
    assert "macos" not in job["runs-on"]
