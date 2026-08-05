"""Tests for diagnose_capture and the three trust tools, through FastMCP's own dispatch.

SPEC.md calls `diagnose_capture` the highest-value tool in the set and notes it is absent from
all surveyed prior art: three very different causes present identically as "no traffic", and an
agent that cannot tell them apart guesses. So these tests care less about the envelope than
about whether each cause reaches the right status with a remediation that can be acted on.

The trust tools carry a deliberate asymmetry (SPEC.md, "Trust"). A simulator's trust store is
per-device, disposable, needs no privilege and is wiped by `simctl erase`, so installing there
is inherent to `start_capture`. The macOS system keychain is none of those things, so it is a
separate tool that is never implicit.
"""

from __future__ import annotations

import json

import pytest

from fake_commands import FakeCommands
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets.simctl import authority_digest, ensure_authority
from stub_runtime import StubRuntime
from test_ios_target import BOOTED_UDID, SHUTDOWN_UDID, devices_json
from test_simctl import MODERN_STORE, write_trust_store
from test_tools_exchanges import captured
from tool_calls import call


@pytest.fixture
def commands(tmp_path):
    return FakeCommands({("xcrun", "simctl", "list", "devices"): devices_json(tmp_path)})


@pytest.fixture
async def state(tmp_path, commands):
    state = CaptureState(tmp_path, runtime_factory=StubRuntime, runner=commands)
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
def server(state):
    return build_server(state=state)


def trust_the_authority(tmp_path):
    """Put the authority's digest in the device's trust store, as a real install would."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    write_trust_store(tmp_path / "device" / MODERN_STORE, [authority_digest(pem)])


# --- diagnose_capture ---------------------------------------------------------


async def test_diagnosing_before_starting_says_so_rather_than_failing(server):
    """This is the tool an agent reaches for when it is already confused. Raising here would
    answer a question about why nothing works with another thing that does not work."""
    result = await call(server, "diagnose_capture")

    assert result["status"] == "no_connect"
    assert "start_capture" in result["remediation"]


async def test_a_curl_session_diagnoses_ok(server, state):
    await state.start(target="curl", port=0, scope=None)

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok"


async def test_an_untrusted_simulator_is_diagnosed_as_such(server, state, tmp_path):
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)

    result = await call(server, "diagnose_capture")

    assert result["status"] == "untrusted_authority"
    assert "install_authority" in result["remediation"]


async def test_a_trusted_simulator_with_traffic_diagnoses_ok(server, state, tmp_path):
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok"


async def test_pinning_is_diagnosed_as_tls_failed_not_as_no_traffic(server, state, tmp_path):
    """PLAN.md's risk table requires pinning be reported as pinning. Handshakes failing while
    the device *does* trust the authority is the only signature available.

    Requires a Scope, and the live P3 run is why; see the last section of this file.
    """
    trust_the_authority(tmp_path)
    await state.start(
        target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID
    )
    state.store.record_tls_failure("api.bank.example", "certificate rejected by client")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "tls_failed"
    assert "pinning" in result["remediation"].lower()


async def test_traffic_hidden_by_the_scope_is_diagnosed_out_of_scope(server, state, tmp_path):
    """Traffic arrived and the Scope reported none of it. Reporting "no traffic" would send the
    agent to debug routing that is working perfectly."""
    trust_the_authority(tmp_path)
    await state.start(
        target="ios-simulator", port=0, scope=["nothing.example.com"], udid=BOOTED_UDID
    )
    state.store.record(captured(host="api.example.com"))

    result = await call(server, "diagnose_capture")

    assert result["status"] == "out_of_scope"


async def test_the_diagnosis_reports_the_counts_it_reasoned_from(server, state, tmp_path):
    """So an agent can check the reasoning rather than having to trust the verdict."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    state.store.record_tls_failure("other.example", "boom")

    result = await call(server, "diagnose_capture")

    assert result["observed"]["exchanges"] == 1
    # Unscoped, so the failure is real but unattributable: it counts in the total and is
    # deliberately not treated as evidence about the app under test.
    assert result["observed"]["tls_failures_total"] == 1
    assert result["observed"]["tls_failures"] == 0


# --- get_trust_status ---------------------------------------------------------


async def test_trust_status_can_be_asked_without_a_session(server, tmp_path):
    """An agent should be able to check trust before committing to a capture."""
    trust_the_authority(tmp_path)

    result = await call(server, "get_trust_status", udid=BOOTED_UDID)

    assert result["trusted"] is True


async def test_trust_status_reports_an_untrusted_device(server):
    result = await call(server, "get_trust_status", udid=BOOTED_UDID)

    assert result["trusted"] is False


async def test_trust_status_names_the_device_it_answered_about(server, tmp_path):
    """With two booted simulators on this machine, an answer that does not say which device it
    is about is not an answer."""
    result = await call(server, "get_trust_status", udid=BOOTED_UDID)

    assert BOOTED_UDID in result["detail"]


# --- install_authority --------------------------------------------------------


async def test_installing_the_authority_reports_the_result(server, commands, tmp_path):
    result = await call(server, "install_authority", udid=BOOTED_UDID)

    assert any("add-root-cert" in part for argv in commands.issued for part in argv)
    assert "trusted" in result


async def test_installing_into_a_shut_down_device_explains_the_remedy(server):
    result = await call(server, "install_authority", udid=SHUTDOWN_UDID)

    assert result["trusted"] is False
    assert "boot" in result["detail"].lower()


# --- install_authority_system -------------------------------------------------


async def test_the_system_install_never_waits_for_a_password(server, commands):
    """The critical property. This server speaks JSON-RPC over stdin, so a sudo prompt would
    read the client's next message as a password and hang the session forever. sudo is invoked
    with -n so it fails immediately instead of prompting.
    """
    await call(server, "install_authority_system")

    sudo_calls = [argv for argv in commands.issued if argv and argv[0] == "sudo"]
    assert sudo_calls, "expected the tool to attempt a sudo call"
    assert all("-n" in argv for argv in sudo_calls)


async def test_a_refused_system_install_hands_back_a_command_to_run(tmp_path):
    """Without cached credentials `sudo -n` fails, and the only useful answer is the exact
    command a human can paste — not a traceback."""

    class RefusingSudo(FakeCommands):
        def __call__(self, argv, *, stdin=None):
            if argv and argv[0] == "sudo":
                raise OSError("sudo: a password is required")
            return super().__call__(argv, stdin=stdin)

    refusing = RefusingSudo({("xcrun", "simctl", "list", "devices"): devices_json(tmp_path)})
    server = build_server(
        state=CaptureState(tmp_path, runtime_factory=StubRuntime, runner=refusing)
    )

    result = await call(server, "install_authority_system")

    assert result["installed"] is False
    assert "add-trusted-cert" in result["command"]
    assert "sudo" in result["command"]


async def test_a_successful_system_install_says_so(server):
    result = await call(server, "install_authority_system")

    assert result["installed"] is True


async def test_the_system_install_is_a_separate_tool_from_the_simulator_one(server):
    """Never implicit: it needs sudo and it is machine-wide, so start_capture must not reach
    for it (SPEC.md, "Trust")."""
    tools = {tool.name for tool in await server.list_tools()}

    assert {"install_authority", "install_authority_system"} <= tools


# --- start_capture accepts a udid --------------------------------------------


async def test_starting_a_simulator_capture_passes_the_udid_through(server, state, tmp_path):
    trust_the_authority(tmp_path)

    result = await call(
        server, "start_capture", target="ios-simulator", port=0, udid=BOOTED_UDID
    )

    assert result["trust"]["trusted"] is True


async def test_the_session_manifest_records_the_device(server, state, tmp_path):
    """So a Session directory found later says which simulator it came from."""
    result = await call(
        server, "start_capture", target="ios-simulator", port=0, udid=BOOTED_UDID
    )
    manifest = json.loads((tmp_path / "sessions" / result["session"] / "session.json").read_text())

    assert manifest["udid"] == BOOTED_UDID


# --- handshake failures from OS chatter must not poison the verdict -------------
#
# Found by the live acceptance run against a real simulator, and it invalidated the diagnosis
# on any real Mac. Apple pins its own services, so a booted simulator produces a steady stream
# of failed handshakes for configuration.apple.com, wps.apple.com and api-safari-*.smoot.apple.com
# no matter what the app under test is doing. Judging on the raw count reported tls_failed
# forever, which is the worst possible failure for the tool SPEC.md calls the highest-value one.

#: The exact hosts observed failing during the P3 live run.
APPLE_PINNED = ("configuration.apple.com", "wps.apple.com", "api-safari-asae1b.smoot.apple.com")


async def test_pinned_os_chatter_does_not_make_a_working_capture_look_broken(
    server, state, tmp_path
):
    """The app's traffic is arriving. Apple's own pinned services failing is background noise on
    every Mac, and reporting it as the diagnosis sends the agent to debug nothing."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    for host in APPLE_PINNED:
        state.store.record_tls_failure(host, "client does not trust the proxy's certificate")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok", result


async def test_pinning_in_the_app_under_test_is_still_reported(server, state, tmp_path):
    """The narrowing must not blind the tool to real pinning — inside a Scope a failing host is
    the answer, and denylisted chatter alongside it is still excluded.

    Scoped because an unscoped verdict is not trustworthy; see the last section of this file.
    """
    trust_the_authority(tmp_path)
    await state.start(
        target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID
    )
    state.store.record(captured(host="api.example.com"))
    state.store.record_tls_failure("configuration.apple.com", "noise")
    state.store.record_tls_failure("api.bank.example", "certificate rejected by client")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "tls_failed"
    assert "api.bank.example" in result["detail"]
    assert "configuration.apple.com" not in result["detail"]


async def test_a_scope_also_narrows_which_handshake_failures_matter(server, state, tmp_path):
    """A Scope is a statement about what the agent cares about. A host outside it failing is as
    irrelevant as a host outside it succeeding."""
    trust_the_authority(tmp_path)
    await state.start(
        target="ios-simulator", port=0, scope=["api.example.com"], udid=BOOTED_UDID
    )
    state.store.record(captured(host="api.example.com"))
    state.store.record_tls_failure("telemetry.vendor.example", "pinned")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok", result


async def test_the_full_failure_count_is_still_reported(server, state, tmp_path):
    """Filtering the verdict is not the same as hiding the evidence: the total stays visible so
    an agent can see that handshakes are failing somewhere."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    for host in APPLE_PINNED:
        state.store.record_tls_failure(host, "noise")

    result = await call(server, "diagnose_capture")

    assert result["observed"]["tls_failures_total"] == 3
    assert result["observed"]["tls_failures"] == 0


async def test_an_unattributable_handshake_failure_does_not_drive_the_verdict(
    server, state, tmp_path
):
    """A failure with no SNI cannot be tied to the app under test, and in regular proxy mode the
    machine's other traffic is indistinguishable from the simulator's (ADR-0002) — so treating
    it as evidence recreates exactly the false tls_failed this section exists to prevent."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    state.store.record_tls_failure(None, "no SNI offered")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok", result
    assert result["observed"]["tls_failures_total"] == 1


# --- pinning can only be judged inside a Scope ---------------------------------
#
# The most consequential finding of the P3 live run, and it is a design constraint rather than a
# bug. Redirecting the system proxy redirects the WHOLE MACHINE (ADR-0002: regular proxy mode has
# no process attribution). The authority is installed into the simulator's trust store only, so
# every other application on the developer's Mac — the observed run produced discord.com,
# s3-figma-force-client-reloads-production.figma.com and unleash.codeium.com — fails its
# handshake against the proxy and is indistinguishable from the app under test.
#
# No denylist can fix that: it would have to enumerate every application a developer might run.
# So an unscoped tls_failed verdict is worthless — it fires on a perfectly working capture. Within
# a Scope the agent has said which hosts are the app under test, and the verdict means something.
# SPEC.md already says host filtering "is not a convenience here, it is the only available
# mechanism"; this is that sentence turning into behaviour.


async def test_an_unscoped_capture_never_blames_pinning(server, state, tmp_path):
    """Other desktop applications failing against the machine-wide proxy is the normal case, not
    evidence about the app under test."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    for host in ("discord.com", "unleash.codeium.com", "s3-figma.figma.com"):
        state.store.record_tls_failure(host, "client does not trust the proxy's certificate")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "ok", result


async def test_an_unscoped_capture_still_mentions_the_failures_and_how_to_judge_them(
    server, state, tmp_path
):
    """Not reporting a verdict is different from hiding the evidence. The count is surfaced and
    the remediation names the one thing that would make it interpretable."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record(captured(host="api.example.com"))
    state.store.record_tls_failure("discord.com", "client does not trust the proxy's certificate")

    result = await call(server, "diagnose_capture")

    assert result["observed"]["tls_failures_total"] == 1
    assert "scope" in (result["detail"] + (result["remediation"] or "")).lower()


async def test_a_scoped_capture_judges_pinning_on_the_hosts_it_was_given(server, state, tmp_path):
    """Inside a Scope the noise from other applications is excluded by construction, so a failure
    really is about the app under test."""
    trust_the_authority(tmp_path)
    await state.start(
        target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID
    )
    state.store.record_tls_failure("discord.com", "unrelated desktop app")
    state.store.record_tls_failure("api.bank.example", "certificate rejected by client")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "tls_failed"
    assert result["observed"]["tls_failures"] == 1
    assert result["observed"]["tls_failures_total"] == 2


# --- get_status reports the proxy state ---------------------------------------


async def test_status_reports_that_the_system_proxy_is_redirected(server, state, tmp_path):
    """SPEC.md lists "proxy state" among what get_status returns, and it was the one field
    missing. It matters more than the others here: a redirected system proxy is the part of a
    Session that outlives the process, so "is my machine currently proxied?" deserves an answer
    that does not require reading a sentinel file by hand."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)

    result = await call(server, "get_status")

    assert result["proxy"]["redirected"] is True
    assert result["proxy"]["service"] == "Wi-Fi"


async def test_status_reports_no_redirection_for_a_target_that_arranges_none(server, state):
    """The curl target routes nothing, so there is no system proxy to report."""
    await state.start(target="curl", port=0, scope=None)

    result = await call(server, "get_status")

    assert result["proxy"]["redirected"] is False


async def test_status_reports_the_tls_failure_counts(server, state, tmp_path):
    """So an agent watching a capture can notice handshakes failing without having to call
    diagnose_capture on a hunch."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record_tls_failure("discord.com", "unrelated desktop app")

    result = await call(server, "get_status")

    assert result["tls_failures"] == 1
