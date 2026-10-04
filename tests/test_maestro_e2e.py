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
names a request that was genuinely still in flight when it began.

That last part used to be *incidental* — Safari's own background chatter during a ~17s assertion —
because a first attempt to force it was recorded as not working: `openLink` to a private-IP URL
never made Safari fetch it, so a hanging sink was never reached and `pending` stayed empty.
**Re-probed on 2026-08-17, that does not reproduce.** `simctl openurl` and Maestro's own `openLink`
both fetch `http://<this machine's LAN address>:<port>/`, the connection arrives at a listener
here, and `openLink` *completes* rather than blocking on a page that never loads — so the
assertion still runs, with the request still open underneath it.

So the flow arranges its own overlap: load a page, open a URL pointed at a socket that accepts and
never answers, then fail an assertion. The Timeline places it because a pending Occurrence has no
end and therefore runs to the end of the window (`correlate/timeline.py`), which is exactly what
puts it in the failing Step's `overlapping`. That is also what makes this test scopable at all —
see `SCOPE` below.

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

#: The URL that never comes back, and the whole reason this test can now be scoped.
#:
#: An address from RFC 5737's documentation range: reserved, so it belongs to nobody, and **not on
#: the local subnet** — which is the part that took three probes to find. A socket on this
#: machine's own LAN address is unusable here: `simctl openurl` and Maestro both fetch it and the
#: connection reaches the listener, but it **never reaches the proxy**. macOS bypasses the proxy
#: for local-subnet addresses whatever `networksetup -getproxybypassdomains` reports, and it
#: reports nothing. Measured: the listener saw the connection while `pending` stayed empty, and an
#: `https://example.com/` control through the same Session was captured.
#:
#: Plain HTTP on purpose. mitmproxy parses a plain-HTTP request line as soon as it arrives, so the
#: request is registered before any upstream connection exists; a `CONNECT` would register nothing
#: until a handshake that is never coming. Plain HTTP is intercepted whatever the Scope says
#: (ADR-0007), but the host is still named below, because *reporting* narrows too and a request the
#: Timeline hides cannot overlap anything.
#:
#: One environmental assumption: this network **drops** traffic to that range rather than answering
#: ICMP unreachable, so the connection hangs — measured still hanging at 20s, against a ~17s
#: assertion. On a network that rejects instead, the request completes at once and the in-flight
#: assertion below fails saying exactly that.
HANG_HOST = "192.0.2.1"
HANG_URL = f"http://{HANG_HOST}/never-answers"

#: What this Session may decrypt, which under Passthrough (ADR-0007) is also all it intercepts.
#:
#: A Scope was impossible here until the overlap became deterministic, and both ways of trying were
#: measured rather than argued. With `["example.com"]` the failing assertion had nothing in flight —
#: the overlap was Safari's own chatter, which a Scope forwards unopened. Adding `apple.com` to keep
#: that chatter was worse: those hosts are not the device's alone, so 50 failed handshakes from the
#: Mac and from the machine's other booted simulators came into scope, and the verdict became
#: `tls_failed`, blaming the app for pinning. Arranging the overlap removes the reason to name
#: anybody else's hosts at all.
#:
#: What that leaves out is the point. Run unscoped, this test intercepted the whole machine and
#: broke `www.figma.com`, `discord.com`, `as.atlassian.com`, `us.i.posthog.com` — and
#: `api.copilot.mobile.dev`, which is **Maestro's own endpoint**, and the most credible explanation
#: for the one flake this test has had.
SCOPE = ["example.com", HANG_HOST]


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


def write_flow(directory, url: str, hang_url: str):
    """A flow that loads a page, starts a request that never comes back, then asserts a falsehood.

    `openLink` rather than typing into the address bar: fewer moving parts, and it does not depend
    on Safari's onboarding state in a freshly created device. The second `openLink` is what makes
    the criterion's second half deterministic, and it works only because that command **completes**
    on a page that never loads — measured, not assumed. The first one stays, because it is what
    produces completed Exchanges for the Run.
    """
    flow = directory / "failing.yaml"
    flow.write_text(
        "appId: com.apple.mobilesafari\n"
        "---\n"
        "- launchApp\n"
        f"- openLink: {url}\n"
        f"- openLink: {hang_url}\n"
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
    flow = write_flow(tmp_path, "https://example.com/", HANG_URL)

    try:
        started = await call(
            server,
            "start_capture",
            target="ios-simulator",
            port=0,
            udid=simulator,
            scope=SCOPE,
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

    assert result["steps"] >= 4, f"expected launchApp, two openLinks and assertVisible: {result}"

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
    # And that it is the request this flow *arranged* to be in flight, still open. Without this
    # the criterion is satisfied by whatever Safari happened to be doing, which is how it stood
    # before — green, and unable to say why.
    assert [item for item in in_flight if item["host"] == HANG_HOST], (
        f"the assertion overlapped something, but not the request to {HANG_HOST}, so the overlap is"
        " incidental again rather than arranged."
        f" overlapping={json.dumps(in_flight, indent=2)}"
    )

    # The sentence the project exists to produce.
    assert "still in flight" in assertion["explanation"], assertion["explanation"]

    assert read_real_state() == before, "the machine's proxy configuration was not restored"
