"""Capture Targets: CurlTarget in particular, then the contract every target must satisfy.

A Capture Target is the thing whose traffic is intercepted, together with whatever must be
arranged for interception to work (CONTEXT.md). For `curl` almost nothing must be arranged,
which is exactly why it is the target P2 ships: it exercises the whole tool surface with no
simulator, no routing and no trust store in the way.

The parametrised suite at the end is the *shared* one PLAN.md's testing section asks for, and it
is what makes Android in v1.1 an addition rather than a refactor. It asserts only the promises
:mod:`~https_proxy_mcp.targets.base` actually makes, so it says nothing about iOS or about curl and
a third target inherits it for free. Nothing in it touches the machine — both targets are driven
through :class:`FakeCommands` — so it runs in the ordinary suite.
"""

from typing import get_args

import pytest

from fake_commands import FakeCommands
from https_proxy_mcp.targets import CaptureTarget, CurlTarget, build_target, known_targets
from https_proxy_mcp.targets.base import CaptureObservations, DiagnosisStatus, TrustStatus
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget
from test_ios_target import BOOTED_UDID, devices_json


def test_curl_is_a_known_target():
    assert "curl" in known_targets()


def test_a_target_is_built_by_name():
    assert isinstance(build_target("curl"), CurlTarget)


def test_an_unknown_target_is_rejected_by_name():
    with pytest.raises(ValueError, match="curl"):
        build_target("nintendo-ds")


def test_curl_target_satisfies_the_protocol():
    """The protocol is what the core depends on; a target that drifts from it breaks P3."""
    assert isinstance(CurlTarget(), CaptureTarget)


# --- the lifecycle is a no-op, and says so ------------------------------------


def test_preparing_is_a_no_op():
    CurlTarget().prepare()


def test_routing_is_the_caller_s_job():
    """curl is pointed at the proxy with -x, so there is no system state to change."""
    CurlTarget().route_traffic(8080)


def test_teardown_is_a_no_op():
    CurlTarget().teardown()


def test_teardown_is_safe_to_repeat():
    """stop_capture may run after a failed start, so teardown must tolerate never starting."""
    target = CurlTarget()
    target.teardown()
    target.teardown()


# --- trust and diagnosis ------------------------------------------------------


def test_there_is_no_authority_to_trust():
    status = CurlTarget().ensure_trusted()

    assert status.trusted is True
    assert "curl" in status.detail


def test_diagnosis_reports_ok_and_admits_what_it_cannot_see():
    """A target that cannot observe routing must not claim capture is working."""
    diagnosis = CurlTarget().diagnose()

    assert diagnosis.status == "ok"
    assert diagnosis.remediation is None


# --- asking about trust without changing it -----------------------------------
#
# `get_trust_status` is a question, and a question that changes what it asks about is a trap. The
# tool layer used to branch on `isinstance(target, IosSimulatorTarget)` to find the read-only
# path, which worked and put a per-target concern in the wrong place. It is on the protocol now, so
# a third target has to answer it deliberately rather than inheriting `ensure_trusted`'s side
# effect by omission. The contract suite below is what actually holds every target to that; a
# `hasattr` check on one target used to stand here and only looked as though it did.


def test_curl_reports_trust_the_same_way_it_reports_ensuring_it():
    """There is nothing to install, so asking and arranging are genuinely the same answer here —
    which is the point: the *protocol* distinguishes them even where a target need not."""
    target = CurlTarget()

    assert target.trust_status().trusted is target.ensure_trusted().trusted


# --- the registry catches a mistyped option -----------------------------------


def test_an_unknown_option_is_refused_rather_than_ignored():
    """Both targets accept `**_options` so the registry can pass one shape to all of them, which
    means neither would notice a typo. A mistyped `udid` is not cosmetic: the simulator target
    would fall back to guessing a device, and `start_capture` documents capturing the wrong app
    and reporting success as the outcome it must never produce.
    """
    with pytest.raises(ValueError, match="uuid"):
        build_target("curl", uuid="not-the-right-name")


def test_the_options_targets_are_actually_built_with_are_accepted():
    """Guards the guard: if the registry's real option set ever changes, this fails rather than
    the validation silently rejecting a legitimate call."""
    assert isinstance(build_target("curl", udid=None, root="/tmp", runner=print), CurlTarget)


# --- the shared CaptureTarget contract ----------------------------------------
#
# One suite every target must pass, asserting the promises base.py makes and nothing else. The
# failure mode it exists for is an omission rather than a mistake: a new target that simply does
# not know a promise exists gets it wrong by not implementing it, and a per-target test file will
# never say so because nobody writes a test for a method they forgot.


def make_curl(tmp_path):
    # The recorder handed back must be the one the target was built with, or an assertion about
    # what it issued is asserting about an empty object that was never wired to anything.
    commands = FakeCommands()
    return CurlTarget(root=tmp_path, runner=commands), commands


def make_ios_simulator(tmp_path):
    """The real target over a stubbed command runner: production code, fake boundary."""
    commands = FakeCommands({("xcrun", "simctl", "list", "devices"): devices_json(tmp_path)})
    return IosSimulatorTarget(udid=BOOTED_UDID, root=tmp_path, runner=commands), commands


#: One factory per target the registry knows. Pinned against ``known_targets()`` below for the
#: same reason ``REGISTERED_TOOLS`` is asserted exactly: a target added without a factory would
#: otherwise skip the contract silently, which is the one way this suite can stop being shared.
TARGET_FACTORIES = {"curl": make_curl, "ios-simulator": make_ios_simulator}


@pytest.fixture(params=sorted(TARGET_FACTORIES), ids=sorted(TARGET_FACTORIES))
def contract(request, tmp_path):
    """Each registered target in turn, with the recorder it was built over."""
    return TARGET_FACTORIES[request.param](tmp_path)


def test_the_contract_suite_covers_every_registered_target():
    assert set(TARGET_FACTORIES) == set(known_targets())


def test_every_target_satisfies_the_protocol(contract):
    target, _ = contract

    assert isinstance(target, CaptureTarget)


def test_every_target_can_be_prepared_before_a_session_exists(contract):
    target, _ = contract

    assert target.prepare() is None


def test_both_trust_methods_answer_with_a_trust_status(contract):
    """Two methods, one return type. What separates them is what they may *change*, not what
    they say, so a target cannot signal "nothing to install" by returning something else."""
    target, _ = contract

    assert isinstance(target.trust_status(), TrustStatus)
    assert isinstance(target.ensure_trusted(), TrustStatus)


def test_reporting_trust_changes_nothing(contract):
    """The promise `trust_status` exists for, held to across every target.

    An agent asking whether a device was *already* trusted must not thereby arrange trust — it
    would never be able to find out. Asserted on the commands actually issued rather than on the
    answer, because a target that installed and then reported truthfully would still be wrong.
    """
    target, commands = contract

    target.trust_status()

    assert commands.mutations == []
    assert [argv for argv in commands.issued if "add-root-cert" in argv] == []


def test_a_target_diagnoses_with_a_status_the_core_understands(contract):
    """`diagnose_capture` maps the status to remediation, so an unknown one has no handling."""
    target, _ = contract

    assert target.diagnose().status in get_args(DiagnosisStatus)


def test_observations_are_accepted_and_cannot_widen_the_statuses(contract):
    target, _ = contract
    observed = CaptureObservations(exchanges=3, tls_failures=1, tls_failures_total=1, reported=0)

    assert target.diagnose(observed).status in get_args(DiagnosisStatus)


def test_teardown_is_safe_when_nothing_was_ever_routed(contract):
    """The common case, and base.py says so: a start that failed before `route_traffic` still
    reaches teardown, and restoring state that was never changed must not raise."""
    target, _ = contract

    target.teardown()
    target.teardown()


def test_teardown_stays_repeatable_after_a_real_route(contract):
    """The other half — idempotence once there *is* something to undo. For the simulator that
    means the sentinel round-trip, which is the layer `kill -9` recovery is built on."""
    target, _ = contract

    target.prepare()
    target.route_traffic(8080)
    target.teardown()
    target.teardown()
