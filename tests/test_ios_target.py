"""Tests for the iOS Simulator Capture Target — the lifecycle, and the diagnosis.

Nothing here touches the machine or a real simulator: the target is driven with the real
:class:`Simctl` and the real :class:`SystemProxy`, both over a stubbed command runner, so the
code under test is production code and only the boundary is fake.

The diagnosis tests are the point of the file. SPEC.md calls `diagnose_capture` the
highest-value tool in the set because so many different things present identically as "no
traffic", and the whole value is lost if the statuses are merely plausible rather than
correct. Each test below pins one of the six verdicts to the condition that produces it.
"""

import json
import os
import re
from dataclasses import replace

import pytest

from fake_commands import PROCESS_START, FakeCommands
from https_proxy_mcp.targets import CaptureTarget, build_target, known_targets
from https_proxy_mcp.targets.base import CaptureObservations
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget
from https_proxy_mcp.targets.simctl import authority_digest, ensure_authority
from test_sentinel import write_sentinel
from test_simctl import MODERN_STORE, write_trust_store

BOOTED_UDID = "F48AAE9F-4D3F-4817-9050-7B4B0024CF07"
SHUTDOWN_UDID = "BD3912F6-630E-4A02-9519-34D1D4787227"


def devices_json(tmp_path, *, booted=True):
    """simctl output for one device whose data directory lives under tmp_path."""
    return json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-26-2": [
                    {
                        "udid": BOOTED_UDID,
                        "name": "iPhone 17 Pro - Sidekick",
                        "state": "Booted" if booted else "Shutdown",
                        "isAvailable": True,
                        "dataPath": str(tmp_path / "device" / "data"),
                    },
                    {
                        "udid": SHUTDOWN_UDID,
                        "name": "Sidekick - iPhone 17 Pro Max",
                        "state": "Shutdown",
                        "isAvailable": True,
                        "dataPath": str(tmp_path / "other" / "data"),
                    },
                ]
            }
        }
    )


@pytest.fixture
def root(tmp_path):
    return tmp_path


def build(tmp_path, *, udid=BOOTED_UDID, booted=True):
    """A target plus the fake it is driven through."""
    listing = devices_json(tmp_path, booted=booted)
    commands = FakeCommands({("xcrun", "simctl", "list", "devices"): listing})
    target = IosSimulatorTarget(udid=udid, root=tmp_path, runner=commands)
    return target, commands


def trust_the_authority(tmp_path):
    """Put the authority's digest in the device's trust store, as a real install would."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    write_trust_store(tmp_path / "device" / MODERN_STORE, [authority_digest(pem)])


# --- registration -------------------------------------------------------------


def test_the_simulator_is_a_known_target():
    assert "ios-simulator" in known_targets()


def test_the_simulator_target_satisfies_the_protocol(tmp_path):
    target, _ = build(tmp_path)

    assert isinstance(target, CaptureTarget)


def test_the_simulator_is_built_by_name_with_its_options(tmp_path):
    """`start_capture` passes udid through the registry, so the registry has to carry options."""
    target = build_target("ios-simulator", udid=BOOTED_UDID, root=tmp_path)

    assert isinstance(target, IosSimulatorTarget)


def test_the_curl_target_still_builds_with_no_options():
    """Targets share one constructor shape; the trivial one must not have become harder."""
    assert build_target("curl") is not None


# --- trust --------------------------------------------------------------------


def test_ensuring_trust_installs_the_authority(tmp_path):
    target, commands = build(tmp_path)

    target.ensure_trusted()

    assert any("add-root-cert" in part for argv in commands.issued for part in argv)


def test_ensuring_trust_generates_the_authority_when_there_is_none(tmp_path):
    """ensure_trusted runs before the proxy starts, so on a first run nothing has made one."""
    target, _ = build(tmp_path)

    target.ensure_trusted()

    assert (tmp_path / "mitmproxy" / "mitmproxy-ca-cert.pem").exists()


def test_trust_is_verified_against_the_device_store_not_assumed(tmp_path):
    """The install command exiting 0 is not evidence the device trusts anything — it is
    evidence simctl accepted the file. The trust store is the only real answer."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)

    status = target.ensure_trusted()

    assert status.trusted is True


def test_an_install_that_did_not_take_reports_untrusted(tmp_path):
    """No trust store is written by the fake, so verification must fail rather than claim
    success on the strength of the exit code."""
    target, _ = build(tmp_path)

    status = target.ensure_trusted()

    assert status.trusted is False
    assert "trust" in status.detail.lower()


def test_a_shut_down_device_cannot_be_trusted_and_says_why(tmp_path):
    """simctl keychain needs a booted device — established by experiment, HANDOFF.md's open
    question 4. The status has to name the remedy, not just report failure."""
    target, _ = build(tmp_path, udid=SHUTDOWN_UDID, booted=True)

    status = target.ensure_trusted()

    assert status.trusted is False
    assert "boot" in status.detail.lower()


# --- routing and teardown -----------------------------------------------------


def test_routing_redirects_the_system_proxy(tmp_path):
    """CoreSimulator has no proxy setting of its own; it inherits the host's."""
    target, commands = build(tmp_path)

    target.route_traffic(8080)

    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "8080"] in commands.mutations
    assert ["-setsecurewebproxy", "Wi-Fi", "127.0.0.1", "8080"] in commands.mutations


def test_routing_writes_the_sentinel(tmp_path):
    target, _ = build(tmp_path)

    target.route_traffic(8080)

    assert (tmp_path / "proxy-state.json").exists()


def test_teardown_restores_the_system_proxy(tmp_path):
    target, commands = build(tmp_path)
    target.route_traffic(8080)

    target.teardown()

    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations
    assert not (tmp_path / "proxy-state.json").exists()


def test_teardown_after_a_failed_start_changes_nothing(tmp_path):
    """`CaptureState.start` calls teardown on every failure path, including before routing."""
    target, commands = build(tmp_path)

    target.teardown()

    assert commands.mutations == []


def test_teardown_is_safe_to_repeat(tmp_path):
    target, _ = build(tmp_path)
    target.route_traffic(8080)

    target.teardown()
    target.teardown()


def test_teardown_leaves_a_redirect_owned_by_a_live_foreign_process_alone(tmp_path):
    """The stop path, reached the way `stop_capture` reaches it.

    Two editor windows share one sentinel because they share a root, and this is the call that
    used to restore the machine out from under the other one — leaving it recording while
    receiving nothing.
    """
    write_sentinel(
        tmp_path / "proxy-state.json", {"pid": os.getpid() + 1, "started": PROCESS_START.strip()}
    )
    target, commands = build(tmp_path)

    target.teardown()

    assert commands.mutations == []
    assert (tmp_path / "proxy-state.json").exists()


def test_preparing_repairs_a_sentinel_left_by_a_dead_process(tmp_path):
    """Layer 3, reached through the target: without this, redirect() would refuse to start
    because a stale sentinel is still on disk."""
    (tmp_path / "proxy-state.json").write_text(
        json.dumps(
            {
                "service": "Wi-Fi",
                "web": {"enabled": False, "server": "127.0.0.1", "port": 9090},
                "secure": {"enabled": False, "server": "127.0.0.1", "port": 9090},
                "bypass": [],
            }
        )
    )
    target, commands = build(tmp_path)

    target.prepare()

    assert not (tmp_path / "proxy-state.json").exists()
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations


# --- diagnosis ----------------------------------------------------------------


def test_traffic_arriving_is_ok(tmp_path):
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    assert target.diagnose(CaptureObservations(exchanges=12)).status == "ok"


def test_no_routing_is_no_connect(tmp_path):
    """Nothing was redirected, so nothing could possibly arrive."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)

    diagnosis = target.diagnose(CaptureObservations())

    assert diagnosis.status == "no_connect"
    assert "start_capture" in (diagnosis.remediation or "")


def test_a_shut_down_device_is_no_connect(tmp_path):
    target, _ = build(tmp_path, udid=SHUTDOWN_UDID)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations())

    assert diagnosis.status == "no_connect"
    assert "boot" in (diagnosis.remediation or "").lower()


def test_an_untrusted_authority_is_reported_as_such(tmp_path):
    """The single most common cause of "HTTPS shows nothing", and directly observable."""
    target, _ = build(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(tls_failures=3))

    assert diagnosis.status == "untrusted_authority"
    assert "install_authority" in (diagnosis.remediation or "")


def test_handshake_failures_with_a_trusted_authority_are_reported_as_tls_failed(tmp_path):
    """This is the pinning signature, and PLAN.md's risk table requires it be reported as
    pinning rather than as "no traffic"."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(
        CaptureObservations(tls_failures=4, tls_failure_hosts=("api.bank.example",))
    )

    assert diagnosis.status == "tls_failed"
    assert "api.bank.example" in diagnosis.detail
    assert "pinning" in (diagnosis.remediation or "").lower()


# The verdict above was reproduced deliberately on 2026-08-18 and its wording found false. One
# capture Session, two legs, nothing about the device changed in between: leg A drove the real
# app on a device that trusts the Authority and got `ok` with zero failures; leg B made a single
# request to the same in-scope host from an unrelated client on the same Mac, and the verdict
# flipped to tls_failed naming that device. Every client reaches the proxy over loopback, so
# there is no peer address that could separate a simulator from a browser — the count is
# machine-wide and cannot be made per-client. Only the wording can be fixed, so these two tests
# guard the wording. The test above cannot notice either regression: "pinning" survives both.


def test_tls_failed_does_not_claim_the_failing_client_was_the_capture_target(tmp_path):
    """The count is machine-wide and the old detail said "even though <device> trusts", which
    asserts the failing client *was* that device. It is unknowable, and it was wrong on the run
    that produced it: the named device was working perfectly at that moment.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(
        CaptureObservations(tls_failures=1, tls_failure_hosts=("api.bank.example",))
    )

    assert "even though" not in diagnosis.detail.lower()
    assert "machine-wide" in diagnosis.detail.lower()
    # The device is still named, and still described as trusting — as the fact it is, not as the
    # contrast that implicated it.
    assert "iPhone 17 Pro - Sidekick" in diagnosis.detail


def test_tls_failed_offers_pinning_as_the_candidate_and_names_the_cheaper_cause(tmp_path):
    """Pinning was stated as fact and it was the wrong cause. It stays as the leading candidate,
    conditional on the failures being the app's, with the cheaper cause named beside it: only the
    capture target was given the Authority, so any other client on this Mac fails identically.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    remediation = (
        target.diagnose(
            CaptureObservations(tls_failures=1, tls_failure_hosts=("api.bank.example",))
        ).remediation
        or ""
    )

    assert "if the failures are the app's" in remediation.lower()
    assert "browser" in remediation.lower()


def test_traffic_hidden_by_the_scope_is_out_of_scope_not_no_connect(tmp_path):
    """Exchanges were recorded and the Scope reported none of them. Calling that "no traffic"
    sends the agent to debug routing that is working perfectly."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(exchanges=40, reported=0))

    assert diagnosis.status == "out_of_scope"
    assert "scope" in (diagnosis.remediation or "").lower()


def test_routed_and_trusted_but_silent_is_no_connect(tmp_path):
    """Everything on this side is correct and still nothing arrived, so the next thing to
    check is the app, not the setup."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(exchanges=0))

    assert diagnosis.status == "no_connect"


def test_diagnosis_without_observations_does_not_claim_ok(tmp_path):
    """Asked with nothing to go on, the honest answer is not "working"."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    assert target.diagnose().status != "ok"


# --- the proxy is redirected, but not by us -----------------------------------
#
# The two windows of `test_two_windows.py`, asked through the diagnosis instead of through a
# refusal. A live foreign owner is claimed here by a fake process table rather than proved by a
# second process, which is right for these tests and not for those: what is under test is that the
# branch keys on *ownership*, and the ownership arithmetic itself is asserted where it can be —
# against a real process table in `test_two_windows.py`, and state by state in `test_sentinel.py`.


def test_a_redirect_held_by_another_live_process_is_its_own_verdict(tmp_path):
    """The cause this tool could not see, and the remediation shares nothing with the other five.

    Everything on this side is in place — the machine is proxied, the device is booted and trusts
    the authority — and still nothing can arrive, because the machine's traffic is going to another
    process's capture port. Called `no_connect`, that reads as "drive the app harder".
    """
    holder = os.getpid() + 1
    write_sentinel(tmp_path / "proxy-state.json", {"pid": holder, "started": PROCESS_START.strip()})
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)

    diagnosis = target.diagnose(CaptureObservations())

    assert diagnosis.status == "proxy_held_elsewhere"
    assert f"pid {holder}" in diagnosis.detail
    assert f"kill {holder}" in (diagnosis.remediation or "")


def test_the_held_verdict_promises_only_that_the_refusal_lifts(tmp_path):
    """This target's copy of a correction the core's copy already carries, and nothing pinned it.

    Signalling the holder lifts *this* refusal. It does not boot a device or arrange trust, so
    "after which start_capture here succeeds" is a stronger promise than this code can keep — and
    this verdict is read by an agent that is already following a remediation, which is the worst
    place to be over-promised to. Reverting the sentence left the whole suite green.
    """
    holder = os.getpid() + 1
    write_sentinel(tmp_path / "proxy-state.json", {"pid": holder, "started": PROCESS_START.strip()})
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)

    diagnosis = target.diagnose(CaptureObservations())

    assert diagnosis.status == "proxy_held_elsewhere"
    assert "succeeds" not in (diagnosis.remediation or "")
    assert "no longer applies" in (diagnosis.remediation or "")


def test_a_redirect_left_behind_by_a_dead_process_is_held_by_nobody(tmp_path):
    """The other half, and what stops the new verdict swallowing the old ones.

    A record whose owner is gone is a crash leftover, which the repair layer already handles — and
    the difference between it and a live holder is the whole fix. A branch keying on the record
    merely existing would report a machine nobody holds as held, and send the developer hunting for
    a process that is not there.
    """
    write_sentinel(
        tmp_path / "proxy-state.json",
        {"pid": os.getpid() + 1, "started": "Sun Aug  9 09:00:00 2026"},
    )
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)

    assert target.diagnose(CaptureObservations()).status == "no_connect"


# --- what Passthrough lets an empty listing mean ------------------------------


def test_nothing_arrived_says_that_nothing_outside_the_scope_was_decrypted(tmp_path):
    """The accepted regression, turned from a mystifying timeout into an explanation.

    Under Passthrough a host the Scope does not name is forwarded without being opened, so the two
    tools that are deliberately exempt from the Scope — a wait naming a host, and a fetch by id —
    find nothing rather than being vetoed. An agent that is not told this concludes the app made no
    request.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(exchanges=0, passthrough=True))

    assert diagnosis.status == "no_connect"
    assert "Passthrough" in diagnosis.detail
    assert "not evidence" in diagnosis.detail


def test_the_caveat_says_tls_rather_than_claiming_plain_http_was_passed_through(tmp_path):
    """Measured by hand after the caveat was written, and it contradicted it.

    Passthrough scopes **TLS** interception only. mitmproxy decides whether to open a connection
    before it knows the destination of a plain-HTTP proxy request — only a CONNECT authority and a
    TLS SNI supply a name early enough — so plain HTTP is intercepted whatever the Scope says. The
    hand-verification's whole capture was plain-HTTP bundler traffic from a host the Scope never
    named. So "a connection to any other host was forwarded without being opened" over-claims in
    the one direction that matters: an agent told that would stop looking for a plain-HTTP request
    that is sitting in the store, hidden by reporting rather than absent from it.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    detail = target.diagnose(CaptureObservations(exchanges=0, passthrough=True)).detail

    assert "HTTPS" in detail
    # Word-boundaried so it cannot be satisfied by the "HTTPS" already asserted above, and
    # case-insensitive because where the sentence starts is not the subject.
    assert re.search(r"plain HTTP\b", detail, re.IGNORECASE)
    assert "a connection to any other host was forwarded without being opened" not in detail


def test_out_of_scope_says_it_too(tmp_path):
    """The same sentence on the other verdict an agent reaches when a listing is empty."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(exchanges=40, reported=0, passthrough=True))

    assert diagnosis.status == "out_of_scope"
    assert "not evidence" in diagnosis.detail


def test_the_passthrough_caveat_is_a_deduction_and_not_a_measurement(tmp_path):
    """Asserted with nothing observed at all, because that is the only honest premise available.

    Nothing counts passed-through connections and nothing should: the engine's option for observing
    them keeps every such connection's contents in memory, which on a machine-wide proxy is the
    whole machine's traffic. So the sentence has to stand on "Passthrough is on and this host is not
    in the Scope" — a measurement could not produce it here, since there is nothing to measure.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    diagnosis = target.diagnose(CaptureObservations(passthrough=True))

    assert "deduced" in diagnosis.detail
    assert "not evidence" in diagnosis.detail


def test_without_passthrough_both_verdicts_are_worded_exactly_as_before(tmp_path):
    """An addition, not a rewrite. Asserted as a prefix rather than against pinned prose, so it
    fails both ways round: a caveat that failed to appear, and a base sentence reworded under
    Passthrough — which would leave two wordings for one verdict, drifting apart from here on.
    """
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    target.route_traffic(8080)

    for observed in (
        CaptureObservations(exchanges=0),
        CaptureObservations(exchanges=40, reported=0),
    ):
        plain = target.diagnose(observed)
        under_passthrough = target.diagnose(replace(observed, passthrough=True))

        assert under_passthrough.status == plain.status
        assert under_passthrough.detail.startswith(plain.detail)
        assert under_passthrough.detail != plain.detail
        assert under_passthrough.remediation == plain.remediation


# --- the authority census -----------------------------------------------------
#
# Installing the Interception Authority into more than one device is how a second simulator is
# kept working while a capture runs — and it is also how that simulator's traffic starts being
# decrypted, bodies and all, into a Session it is not the Capture Target of. Nothing else in the
# tool output says so, which is why this is counted rather than left to prose.


def both_booted(tmp_path):
    """simctl output with both devices booted, each with its own data directory."""
    listing = json.loads(devices_json(tmp_path))
    for entry in listing["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-2"]:
        entry["state"] = "Booted"
    return json.dumps(listing)


def build_both_booted(tmp_path):
    commands = FakeCommands({("xcrun", "simctl", "list", "devices"): both_booted(tmp_path)})
    return IosSimulatorTarget(udid=BOOTED_UDID, root=tmp_path, runner=commands), commands


def trust_the_other_device(tmp_path):
    """Put the authority in the *second* device's store, as `install_authority(udid=…)` would."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    write_trust_store(tmp_path / "other" / MODERN_STORE, [authority_digest(pem)])


def test_the_census_counts_booted_devices_and_the_ones_that_trust(tmp_path):
    target, _ = build_both_booted(tmp_path)
    trust_the_authority(tmp_path)
    trust_the_other_device(tmp_path)

    census = target.authority_census()

    assert (census.booted, census.trusting) == (2, 2)


def test_the_census_ignores_a_device_that_is_not_booted(tmp_path):
    """Only a booted device can be sending anything, so only a booted one is worth warning about."""
    target, _ = build(tmp_path)
    trust_the_authority(tmp_path)
    trust_the_other_device(tmp_path)

    census = target.authority_census()

    assert (census.booted, census.trusting) == (1, 1)


def test_the_census_says_nothing_when_only_the_capture_target_trusts(tmp_path):
    """The common case, and the one where a note would be noise. A warning that appears every
    time is one an agent learns to skip past on the run where it mattered."""
    target, _ = build_both_booted(tmp_path)
    trust_the_authority(tmp_path)

    census = target.authority_census()

    assert (census.booted, census.trusting) == (2, 1)
    assert census.note(capture_target_trusts=True) is None


def test_the_census_names_the_other_devices_when_they_trust(tmp_path):
    target, _ = build_both_booted(tmp_path)
    trust_the_authority(tmp_path)
    trust_the_other_device(tmp_path)

    note = target.authority_census().note(capture_target_trusts=True)

    assert note is not None
    assert "1 booted device(s) other than this Session's Capture Target" in note
    assert "lens" in note


def test_a_census_that_cannot_be_taken_says_nothing_rather_than_failing(tmp_path):
    """A courtesy beside the answer an agent asked for. Failing `start_capture` over a count that
    could not be read would be absurd, and inventing one is the mistake `tls_failed` was
    corrected for — so an unreadable census is empty, and an empty census has no note."""
    commands = FakeCommands({("xcrun", "simctl", "list", "devices"): "not json"})
    target = IosSimulatorTarget(udid=BOOTED_UDID, root=tmp_path, runner=commands)

    census = target.authority_census()

    assert (census.booted, census.trusting) == (0, 0)
    assert census.note(capture_target_trusts=True) is None
