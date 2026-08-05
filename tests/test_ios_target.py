"""Tests for the iOS Simulator Capture Target — the lifecycle, and the diagnosis.

Nothing here touches the machine or a real simulator: the target is driven with the real
:class:`Simctl` and the real :class:`SystemProxy`, both over a stubbed command runner, so the
code under test is production code and only the boundary is fake.

The diagnosis tests are the point of the file. SPEC.md calls `diagnose_capture` the
highest-value tool in the set because three very different causes present identically as "no
traffic", and the whole value is lost if the statuses are merely plausible rather than
correct. Each test below pins one cause to one status.
"""

import json

import pytest

from fake_commands import FakeCommands
from https_proxy_mcp.targets import CaptureTarget, build_target, known_targets
from https_proxy_mcp.targets.base import CaptureObservations
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget
from https_proxy_mcp.targets.simctl import authority_digest, ensure_authority
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
