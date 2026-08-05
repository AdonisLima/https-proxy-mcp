"""P4's acceptance criterion, for real: a failing Maestro flow, and the Timeline that explains it.

    Done when: a deliberately failing Maestro flow produces a Timeline that shows the failing
    assertion *and* the Exchange still in flight when it began. — PLAN.md

**This file changes the developer's machine.** It redirects the Mac's system proxy, which affects
every application's networking for as long as it runs, so it is gated behind an environment
variable rather than a pytest marker — `-m` on the command line *replaces* the default expression
from `addopts`, so `pytest -m "not integration"` would silently have opted back in:

    HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_maestro_e2e.py -q

The simulator is **created and deleted by the test**. Capture installs an Interception Authority
into the device's trust store, and while that is per-device and disposable, "disposable" is a
property of a device the test made rather than one the developer relies on.

**What this test does and does not prove.** It establishes the real-world integration: a real
failing Maestro flow, a real device, real captured traffic, and a Timeline whose failing assertion
names an Exchange that was genuinely still in flight when it began. The overlap it finds is real
but *incidental* — Safari's own background requests during a ~17s assertion — because a first
attempt to force the overlap deterministically did not work: `openLink` to a private-IP URL never
made Safari fetch it, so a hanging sink was never reached and `pending` stayed empty.

Rather than leave a mechanism that silently does not fire, the deterministic in-flight proof lives
in `test_pending_e2e.py`, which drives the same path through a real proxy with `curl` and needs no
simulator at all. This test is the integration check; that one is the mechanism check.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets.systemproxy import SystemProxy
from tool_calls import call

pytestmark = [
    pytest.mark.integration,
    pytest.mark.machine,
    pytest.mark.skipif(
        os.environ.get("HTTPS_PROXY_MCP_E2E") != "1",
        reason="redirects the machine's real system proxy; set HTTPS_PROXY_MCP_E2E=1 to run",
    ),
]

DEVICE_TYPE = os.environ.get("HTTPS_PROXY_MCP_E2E_DEVICE", "iPhone 17 Pro")

#: Long enough for the simulator to boot, Maestro's driver to install, and a ~17s assertion to
#: exhaust its retries.
FLOW_TIMEOUT_MS = 600_000


@pytest.fixture
def simulator():
    """A throwaway booted simulator, deleted afterwards.

    `simctl create` writes a runtime notice to **stderr**, so the streams are kept apart — folding
    them together puts that notice into the UDID and every later command fails with "Invalid
    device".
    """
    udid = subprocess.run(
        ["xcrun", "simctl", "create", "hpm-p4-e2e", DEVICE_TYPE],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    ).stdout.strip()
    try:
        subprocess.run(["xcrun", "simctl", "bootstatus", udid, "-b"], check=True, timeout=300)
        yield udid
    finally:
        subprocess.run(["xcrun", "simctl", "shutdown", udid], capture_output=True, timeout=120)
        subprocess.run(["xcrun", "simctl", "delete", udid], capture_output=True, timeout=120)


def read_real_state() -> object:
    """The machine's actual proxy configuration, through the real binaries."""
    return SystemProxy("/nonexistent-sentinel").read_state()


def write_flow(directory, url: str):
    """A flow that loads a URL and then asserts something that cannot be true.

    `openLink` rather than typing into the address bar: fewer moving parts, and it does not depend
    on Safari's onboarding state in a freshly created device.
    """
    flow = directory / "failing.yaml"
    flow.write_text(
        "appId: com.apple.mobilesafari\n"
        "---\n"
        "- launchApp\n"
        f"- openLink: {url}\n"
        '- assertVisible: "ThisTextIsDefinitelyNotOnScreen"\n'
    )
    return flow


async def test_a_failing_maestro_flow_produces_an_explanatory_timeline(tmp_path, simulator):
    """The acceptance criterion, end to end, against a real device and a real Maestro.

    One test rather than two, and one simulator: everything here is an assertion about the *same*
    Run, and splitting it would double the only runtime in this project that redirects the
    developer's networking.
    """
    before = read_real_state()
    state = CaptureState(tmp_path)
    server = build_server(state=state)
    flow = write_flow(tmp_path, "https://example.com/")

    try:
        started = await call(
            server, "start_capture", target="ios-simulator", port=0, udid=simulator
        )
        assert started["trust"]["trusted"] is True, started["trust"]["detail"]

        result = await call(
            server,
            "run_test",
            command=["maestro", "test", "--udid", simulator, "--no-ansi", str(flow)],
            label="deliberately failing flow",
            timeout_ms=FLOW_TIMEOUT_MS,
        )

        # Maestro exits 1 on a failing flow. Anything else means the flow did not run at all,
        # which is a different failure and worth saying so.
        assert result["timed_out"] is False, result["stdout_tail"]
        assert result["exit_code"] == 1, (
            f"expected a failing flow (exit 1), got {result['exit_code']}."
            f" stdout:\n{result['stdout_tail']}\nstderr:\n{result['stderr_tail']}"
        )

        timeline = await call(server, "get_timeline", run_id=result["run"], verbosity="full")
        diagnosis = await call(server, "diagnose_capture")
    finally:
        if state.recording:
            await call(server, "stop_capture")

    # --- capture really worked ----------------------------------------------

    assert diagnosis["status"] == "ok", diagnosis
    assert timeline["counts"]["exchanges"] > 0, (
        "no traffic was attributed to the Run, so the window or the narrowing is wrong;"
        f" diagnose_capture says {diagnosis}"
    )

    # --- the Steps came from the real artifact ------------------------------

    assert result["steps"] >= 3, f"expected launchApp, openLink and assertVisible: {result}"

    failing = [item for item in timeline["steps"] if item["status"] == "failed"]
    assert failing, (
        "no failing Step in the Timeline, so the Maestro artifact was not parsed."
        f" Steps: {json.dumps(timeline['steps'], indent=2)}"
    )

    assertion = failing[-1]
    assert "ThisTextIsDefinitelyNotOnScreen" in assertion["label"]
    # Parsed from real output with the 33 KB UI hierarchy discarded at the boundary.
    assert assertion["error"] and len(assertion["error"]) < 500, assertion["error"]

    # --- and something was still in flight when it began --------------------

    in_flight = assertion.get("overlapping", [])
    assert in_flight, (
        "the failing assertion had nothing in flight when it began, so the criterion's second"
        " half is unproven for a real device. The mechanism itself is covered deterministically"
        " by test_pending_e2e.py; this assertion is about the real thing happening."
        f" pending={json.dumps(timeline['pending'])};"
        f" background={timeline['background']['count']}."
        f" Full assertion row: {json.dumps(assertion, indent=2)}"
    )

    # The sentence the project exists to produce.
    assert "still in flight" in assertion["explanation"], assertion["explanation"]

    assert read_real_state() == before, "the machine's proxy configuration was not restored"
