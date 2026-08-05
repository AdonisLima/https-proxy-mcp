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
    the device *does* trust the authority is the only signature available."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
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
    assert result["observed"]["tls_failures"] == 1


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
