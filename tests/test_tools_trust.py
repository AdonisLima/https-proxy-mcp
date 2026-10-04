"""Tests for diagnose_capture and the three trust tools, through FastMCP's own dispatch.

SPEC.md calls `diagnose_capture` the highest-value tool in the set and notes it is absent from
all surveyed prior art: very different causes present identically as "no traffic", and an
agent that cannot tell them apart guesses. Deliberately uncounted here — the count belongs where a
test pins it to `DiagnosisStatus`, and this file is not one of those places. So these tests care
less about the envelope than about whether each cause reaches the right status with a remediation
that can be acted on.

The trust tools carry a deliberate asymmetry (SPEC.md, "Trust"). A simulator's trust store is
per-device, disposable, needs no privilege and is wiped by `simctl erase`, so installing there
is inherent to `start_capture`. The macOS system keychain is none of those things, so it is a
separate tool that is never implicit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fake_commands import PROCESS_START, FakeCommands
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets.simctl import authority_digest, ensure_authority
from stub_runtime import StubRuntime
from test_ios_target import BOOTED_UDID, SHUTDOWN_UDID, devices_json
from test_sentinel import write_sentinel
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


async def test_every_diagnosis_reports_the_same_keys_whether_or_not_a_session_exists(
    server, state, tmp_path
):
    """One shape, so a caller can read a field without first working out which branch answered.

    The not-recording branches omitted ``target``, which the recording branch carries and the
    reference documents — and ``proxy_held_elsewhere`` is the first verdict an agent is likely to
    meet in that shape, because it is reached from a refused ``start_capture``. A ``KeyError`` in
    the branch that exists to say something is wrong is how an agent concludes nothing is wrong.

    Three branches rather than two, because "not recording" is two separate returns and only one
    of them was reached here: dropping ``target`` from the held branch left the whole suite green.
    That is the branch an agent meets while following a remediation, so it is the more expensive
    of the two to get wrong. Its status is asserted as well, since a sentinel written where
    nothing reads it would exercise the *other* not-recording branch twice and still pass.
    """
    sentinel = tmp_path / "proxy-state.json"
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})
    held = await call(server, "diagnose_capture")
    sentinel.unlink()

    before = await call(server, "diagnose_capture")
    await state.start(target="curl", port=0, scope=None)

    assert held["status"] == "proxy_held_elsewhere"
    assert set(held) == set(before) == set(await call(server, "diagnose_capture"))


async def test_the_held_proxy_verdict_does_not_name_a_session_that_does_not_exist(server, tmp_path):
    """The branch is entered only when nothing is recording here, so it has no Session to name.

    It borrowed ``get_status``'s sentence for the same ownership state, which reads "This Session
    does not own it, so stopping here leaves it in place" — and "stopping here" names a call that
    would raise ``NOT_RECORDING``. CONTEXT.md's Session has a precise meaning and there is none
    here; this branch exists because the tool was saying things that were not so.
    """
    holder = os.getpid() + 1
    write_sentinel(tmp_path / "proxy-state.json", {"pid": holder, "started": PROCESS_START.strip()})

    result = await call(server, "diagnose_capture")

    assert result["status"] == "proxy_held_elsewhere"
    # The whole noun, not its capitalisation: lowercasing the claim would leave it just as wrong.
    assert "Session" not in result["detail"]
    assert "stopping here" not in result["detail"]
    # The actionable half is the reason the verdict exists, so it is asserted in the same breath:
    # dropping the wrong clause must not take the pid and the remedy with it.
    assert f"pid {holder}" in result["detail"]
    assert f"kill {holder}" in result["remediation"]


async def test_the_held_proxy_verdict_promises_only_that_the_refusal_lifts(server, tmp_path):
    """Releasing the holder removes *this* refusal. It does not boot a simulator or arrange trust,
    so "after which start_capture here succeeds" is a stronger promise than the code can keep —
    and this branch is reached by an agent following a remediation, which is the worst place to
    over-promise."""
    write_sentinel(
        tmp_path / "proxy-state.json",
        {"pid": os.getpid() + 1, "started": PROCESS_START.strip()},
    )

    result = await call(server, "diagnose_capture")

    assert "succeeds" not in result["remediation"]
    assert "no longer applies" in result["remediation"]


async def test_the_held_proxy_verdict_resolves_ownership_once(server, tmp_path, commands):
    """Two reads of one fact, and the holder could exit between them.

    The verdict asked who the holder was and then asked again to compose the sentence, so a holder
    that exited in between produced ``proxy_held_elsewhere`` carrying the *dead owner* sentence —
    an answer that contradicts itself. Counted through the runner, which is the only place the
    second read was visible from.
    """
    write_sentinel(
        tmp_path / "proxy-state.json",
        {"pid": os.getpid() + 1, "started": PROCESS_START.strip()},
    )

    await call(server, "diagnose_capture")

    reads = [argv for argv in commands.issued if argv[0].endswith("ps")]

    assert reads != [], "ownership was never resolved at all, so this proved nothing"
    assert len(reads) == 1


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
    await state.start(target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID)
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


async def test_a_scoped_session_is_told_that_passthrough_may_be_the_reason(server, state, tmp_path):
    """The Session's own setting reaching the verdict, through the whole chain rather than as an
    argument handed to the target by a test.

    This is the call an agent makes when `wait_for_exchanges` timed out on a host it did not put in
    the Scope. Under Passthrough that host was never decrypted, so the wait could only ever time
    out — and "nothing arrived" on its own reads as "the app made no request".
    """
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=["api.example.com"], udid=BOOTED_UDID)

    result = await call(server, "diagnose_capture")

    assert result["status"] == "no_connect"
    assert "not evidence" in result["detail"]
    assert result["observed"]["passthrough"] is True


async def test_an_unscoped_session_says_nothing_about_passthrough(server, state, tmp_path):
    """Inert without a Scope, and silent about itself when inert: everything through the proxy was
    intercepted, so an empty listing means what it has always meant."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)

    result = await call(server, "diagnose_capture")

    assert "Passthrough" not in result["detail"]
    assert result["observed"]["passthrough"] is False


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

    result = await call(server, "start_capture", target="ios-simulator", port=0, udid=BOOTED_UDID)

    assert result["trust"]["trusted"] is True


async def test_the_session_manifest_records_the_device(server, state, tmp_path):
    """So a Session directory found later says which simulator it came from."""
    result = await call(server, "start_capture", target="ios-simulator", port=0, udid=BOOTED_UDID)
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
    await state.start(target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID)
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
    await state.start(target="ios-simulator", port=0, scope=["api.example.com"], udid=BOOTED_UDID)
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
    """Inside a Scope the noise from other applications talking to *other* hosts is excluded by
    construction, which is what makes the verdict worth reporting at all.

    It is not enough to make the verdict about the app: another client on this same Mac talking
    to an in-scope host is still counted, because every client reaches the proxy over loopback.
    That is what the next test is about.
    """
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID)
    state.store.record_tls_failure("discord.com", "unrelated desktop app")
    state.store.record_tls_failure("api.bank.example", "certificate rejected by client")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "tls_failed"
    assert result["observed"]["tls_failures"] == 1
    assert result["observed"]["tls_failures_total"] == 2


async def test_the_tls_failed_verdict_an_agent_reads_admits_the_count_is_machine_wide(
    server, state, tmp_path
):
    """The unit test pins the string; this pins what actually crosses the tool boundary, which is
    the only text an agent ever sees.

    Reproduced on 2026-08-18: one unrelated client on this Mac making one request to an in-scope
    host flipped a healthy capture to `tls_failed`, naming a device that was working perfectly.
    The Scope narrows hosts, not clients, and nothing can narrow clients — loopback leaves no peer
    address to tell them apart.
    """
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=["api.bank.example"], udid=BOOTED_UDID)
    state.store.record_tls_failure("api.bank.example", "certificate rejected by client")

    result = await call(server, "diagnose_capture")

    assert result["status"] == "tls_failed"
    assert "even though" not in result["detail"].lower()
    assert "machine-wide" in result["detail"].lower()
    assert "if the failures are the app's" in result["remediation"].lower()


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


async def test_status_reports_the_proxy_even_when_nothing_is_recording(server, tmp_path):
    """The question that cost a whole debugging session, and the answer it was denied.

    Proxy state is read from the sentinel, which is process-independent, so it was always
    answerable — it was simply not reported unless this Session happened to be recording, which
    is exactly when nobody needs to ask. A window that has gone away leaves this behind.
    """
    write_sentinel(tmp_path / "proxy-state.json", {"pid": 4242, "started": PROCESS_START.strip()})

    result = await call(server, "get_status")

    assert result["recording"] is False
    assert result["proxy"]["redirected"] is True
    assert result["proxy"]["service"] == "Wi-Fi"


async def test_the_usage_guide_names_every_key_the_proxy_report_returns(server, tmp_path):
    """The redirect report is what answers "is my machine still proxied, and by whom?", and it is
    the one report a developer reads rather than an agent. A key the reference never shows is a key
    nobody looks for — the owner most of all, because it is what decides whether ``stop_capture``
    will fix anything."""
    usage = (Path(__file__).resolve().parent.parent / "docs" / "USAGE.md").read_text()
    write_sentinel(tmp_path / "proxy-state.json", {"pid": 4242, "started": PROCESS_START.strip()})

    reported = await call(server, "get_status")

    assert sorted(key for key in reported["proxy"] if f'"{key}"' not in usage) == []


async def test_status_names_the_owner_and_says_when_it_is_this_process(server, state, tmp_path):
    """An agent's next move depends entirely on this: stop_capture fixes a redirect we own and
    nothing else."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)

    result = await call(server, "get_status")

    assert result["proxy"]["owner"] == {"pid": os.getpid(), "ours": True}
    assert "on stop_capture" in result["proxy"]["detail"]


async def test_status_does_not_promise_to_restore_a_redirect_this_process_does_not_own(
    server, tmp_path
):
    """The existing sentence promised restoration on stop_capture unconditionally. For a record
    another live process holds that promise is simply false, and prose that is confidently wrong
    is worse than prose that says nothing — it is what sends the developer to the wrong window."""
    holder = os.getpid() + 1
    write_sentinel(tmp_path / "proxy-state.json", {"pid": holder, "started": PROCESS_START.strip()})

    result = await call(server, "get_status")

    assert result["proxy"]["owner"] == {"pid": holder, "ours": False}
    assert "will be restored to server=" not in result["proxy"]["detail"]
    assert str(holder) in result["proxy"]["detail"]


async def test_status_names_this_session_as_the_one_that_does_not_own_it_while_recording(
    server, state, tmp_path
):
    """The other arm of the same sentence, and the one that has a Session to name.

    "This Session does not own it, so stopping here leaves it in place" is true here and false in
    the branch above, where there is no Session and ``stop_capture`` would raise — which is why
    the sentence branches at all. Only the no-Session arm was pinned, so the condition could be
    inverted and caught in one direction: this Session would be told nothing here owns the
    redirect while it is the one thing that might, and ``stop_capture`` would look like a no-op.

    Reachable with ``curl``, which arranges no routing of its own, so the sentinel it finds can
    only be the other process's.
    """
    holder = os.getpid() + 1
    write_sentinel(tmp_path / "proxy-state.json", {"pid": holder, "started": PROCESS_START.strip()})
    await state.start(target="curl", port=0, scope=None)

    result = await call(server, "get_status")

    assert result["recording"] is True
    assert result["proxy"]["owner"] == {"pid": holder, "ours": False}
    assert "This Session does not own it" in result["proxy"]["detail"]
    # The remedy travels with the claim: the sentence is only worth branching if it still says
    # what to do about the holder either way.
    assert f"kill {holder}" in result["proxy"]["detail"]


async def test_status_reports_a_redirect_left_behind_by_a_process_that_is_gone(server, tmp_path):
    """The state the field report was actually in: proxied by nobody. Reported as not ours, so an
    agent does not read it as a Session it can stop."""
    write_sentinel(
        tmp_path / "proxy-state.json", {"pid": 4242, "started": "Sun Aug 16 09:00:00 2026"}
    )

    result = await call(server, "get_status")

    assert result["proxy"]["owner"] == {"pid": 4242, "ours": False}
    assert "no longer running" in result["proxy"]["detail"]


async def test_status_reports_a_record_that_names_no_owner_without_inventing_one(server, tmp_path):
    """A record from before the owner existed still has to be reportable. Reporting nobody is the
    honest answer, and it is the same fail-safe direction the repair path takes."""
    write_sentinel(tmp_path / "proxy-state.json", None)

    result = await call(server, "get_status")

    assert result["proxy"]["redirected"] is True
    assert result["proxy"]["owner"] is None


async def test_status_reports_the_same_shape_for_a_record_it_cannot_read(server, tmp_path):
    """The one branch whose shape used to differ, and the one place it mattered least to differ in.

    A caller reading ``proxy["owner"]`` or ``proxy["service"]`` got a KeyError here and nowhere
    else — raised in exactly the branch that exists to say something is wrong, which is how an
    agent ends up concluding the machine is not proxied at all. What cannot be known is reported
    as null; the path is reported, because it is what a human needs to go and read the file.
    """
    sentinel = tmp_path / "proxy-state.json"
    sentinel.write_text("{ this is not json")

    result = await call(server, "get_status")

    assert result["proxy"]["redirected"] is True
    assert result["proxy"]["owner"] is None
    assert result["proxy"]["service"] is None
    assert result["proxy"]["sentinel"] == str(sentinel)
    assert "could not be read" in result["proxy"]["detail"]


async def test_status_reports_the_tls_failure_counts(server, state, tmp_path):
    """So an agent watching a capture can notice handshakes failing without having to call
    diagnose_capture on a hunch."""
    trust_the_authority(tmp_path)
    await state.start(target="ios-simulator", port=0, scope=None, udid=BOOTED_UDID)
    state.store.record_tls_failure("discord.com", "unrelated desktop app")

    result = await call(server, "get_status")

    assert result["tls_failures"] == 1
