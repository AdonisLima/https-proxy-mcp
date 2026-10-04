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


def manifest() -> dict:
    with MANIFEST.open("rb") as handle:
        return tomllib.load(handle)


def gate_configuration() -> dict:
    return manifest()["tool"]


def workflow_jobs() -> dict:
    """The CI workflow, read as data. ``yaml`` reaches the suite as a transitive dependency of
    pre-commit; parsing the workflow rather than matching its text means a reformat that keeps
    the promise (a step renamed, a key reordered) does not fail the assertion."""
    with WORKFLOW.open() as handle:
        return yaml.safe_load(handle)["jobs"]


def ci_test_step() -> dict:
    """The single step, anywhere in the workflow, that runs the test suite."""
    steps = [
        step
        for job in workflow_jobs().values()
        for step in job["steps"]
        if "run" in step and "uv run pytest" in step["run"]
    ]
    assert len(steps) == 1
    return steps[0]


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


def test_per_test_timeout_fails_a_hang_before_the_job_timeout():
    """A hung test's only backstop without this is the CI job's ``timeout-minutes: 20`` — on the
    10x-billed macOS runner, one hang spends ~200 charged minutes against a ~200-minute monthly
    allowance. The per-test cap converts that into a fast, located failure, so the cap must be
    present and small relative to the job timeout. 120s is generous against a suite that takes
    under a minute; the assertion pins the order of magnitude rather than the exact number, so
    raising the cap for a legitimately slow test does not fail the gate."""
    timeout = gate_configuration()["pytest"]["ini_options"]["timeout"]

    assert 0 < timeout <= 120


def test_coverage_floor_is_measured_from_the_full_suite_not_aspirational():
    """The floor is insurance against regression, so it exists, sits a couple of points under the
    recorded full-suite measurement rather than at an aspirational target, and is not a round
    number. A floor raised to a round number, or past the measurement, is pressure toward faking
    the machine-mutating modules more elaborately — the exact outcome the floor's comment warns
    against. The standard exclusions keep typing ceremony out of what the floor measures."""
    report = gate_configuration()["coverage"]["report"]

    assert 0 < report["fail_under"] < 100
    assert report["fail_under"] % 5 != 0
    assert "if TYPE_CHECKING:" in report["exclude_lines"]


def test_the_ci_test_step_carries_the_coverage_flags():
    """The floor is only a gate if something runs with the flags: the CI test step — the full
    suite, not the fast majority — is the one place an instrumented run belongs, since the
    recorded "no tests in pre-commit" decision applies a fortiori to a slower instrumented run.
    Asserted by shape here and exercised by CI on push."""
    run = ci_test_step()["run"]

    assert "--cov=https_proxy_mcp" in run
    assert "--cov-report=" in run


def test_randomized_order_is_a_dev_dependency_not_a_flag():
    """pytest-randomly activates on install — there is no enabling flag — so the dependency
    being in the dev group *is* the adoption, for local runs and CI alike. Asserting the group
    rather than a command line keeps the promise where the plugin's own mechanism keeps it."""
    dev = manifest()["dependency-groups"]["dev"]

    assert any(requirement.startswith("pytest-randomly") for requirement in dev)


def test_the_ci_test_step_runs_randomized_and_reproducible():
    """Adopted only after a clean trial, and only worth anything if a CI failure caused by
    ordering can be replayed: the plugin prints ``--randomly-seed=N`` at run start, and
    pytest's ``-q`` suppresses the header that carries it — measured, not assumed. So the step
    must not pass ``-q``, must not disable the plugin, and must not pin a seed, which would
    turn the shuffle back into a fixed order."""
    run = ci_test_step()["run"]

    assert "-q" not in run.split()
    assert "no:randomly" not in run
    assert "--randomly-seed" not in run


def test_ruff_guards_the_single_event_loop_with_the_async_family():
    """ADR-0001 runs the proxy on the server's single event loop, so a blocking call inside an
    async function stalls capture for every Exchange — the specific bug class ruff's ASYNC
    family detects. ``src/`` measured clean when the family was adopted, so it is a pure guard:
    dropped from the selection, nothing fails until the stall it exists to catch has landed.
    """
    select = gate_configuration()["ruff"]["lint"]["select"]

    assert "ASYNC" in select


def test_async_blocking_in_tests_is_declared_not_silenced():
    """Tests block deliberately — a hanging sink, a subprocess the Run window must outlive — so
    the ASYNC family is narrowed for ``tests/`` by naming the measured rules, not by a blanket
    ``ASYNC`` ignore that would hide a new blocking shape, and not by exempting ``src/``, where
    a hit is a real stall to fix on its merits."""
    ignores = gate_configuration()["ruff"]["lint"]["per-file-ignores"]

    test_ignores = ignores["tests/**"]
    assert "ASYNC" not in test_ignores
    assert {"ASYNC110", "ASYNC220", "ASYNC221", "ASYNC240", "ASYNC251"} <= set(test_ignores)


class _ItemStub:
    """Just enough of ``pytest.Item`` for the collection hook: a marker lookup and a mark sink.

    The hook under test is what scopes the larger timeout to the ``machine`` marker, so the
    assertion has to go through the marker machinery rather than reading a constant off the
    module and trusting it is ever applied.
    """

    def __init__(self, machine: bool) -> None:
        self._machine = machine
        self.added: list = []

    def get_closest_marker(self, name: str):
        return object() if self._machine and name == "machine" else None

    def add_marker(self, marker) -> None:
        self.added.append(marker)


def test_machine_marked_tests_get_a_much_larger_cap_through_the_marker():
    """The machine-gated tests boot real simulators and wait out a 600s flow timeout, so the
    suite-wide cap would kill them for being what they are. The exemption is pytest-timeout's
    marker, which outranks the ini value, applied at collection by the conftest hook — and the
    hook keys on the ``machine`` marker, which stays documentation-only: what keeps these tests
    from redirecting the machine's networking is the HTTPS_PROXY_MCP_E2E skipif on each module,
    and nothing here touches it."""
    import conftest

    machine, plain = _ItemStub(machine=True), _ItemStub(machine=False)
    conftest.pytest_collection_modifyitems([machine, plain])

    assert plain.added == []
    [mark] = machine.added
    assert mark.name == "timeout"
    assert mark.args[0] == conftest.MACHINE_TEST_TIMEOUT_SECONDS
    assert (
        10 * gate_configuration()["pytest"]["ini_options"]["timeout"]
        <= conftest.MACHINE_TEST_TIMEOUT_SECONDS
    )
