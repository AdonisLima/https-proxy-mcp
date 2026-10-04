"""The other half of P3's acceptance criterion: a real simulator app's HTTPS traffic, captured.

**This is the only file in the project that changes the developer's machine.** It redirects the
Mac's system proxy, which affects every application's networking for as long as it runs — not
just the simulator. So it is gated behind an environment variable rather than a pytest marker:

    HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py -q

A marker alone would not be safe enough. `-m` given on the command line *replaces* any default
expression from `addopts`, so `pytest -m "not integration"` — the documented fast-suite command —
would have silently opted back in to redirecting the machine's networking. An environment
variable cannot be re-enabled by accident that way.

If this file is interrupted mid-run, the proxy is restored by the `finally` block; if the
interpreter is killed outright, the next server start repairs it from the sentinel (layer 3). The
manual fallback is in HANDOFF.md.
"""

from __future__ import annotations

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

#: Fetched over HTTPS from inside the simulator. Deliberately not a denylisted host, and
#: deliberately somewhere stable that exists to be fetched.
TARGET_URL = "https://example.com/"
TARGET_HOST = "example.com"

#: What this Session is allowed to decrypt, which under Passthrough (ADR-0007) is also all it
#: intercepts. Everything else is forwarded unopened, so the developer's browser and the other
#: simulators on the machine keep working for as long as the test holds the proxy.
#:
#: These tests were written before that mechanism existed and ran unscoped, which intercepts the
#: whole machine: a real run logged client handshake failures for six of the developer's own
#: applications. Nothing here asserts on any host but this one, so the Scope costs the tests
#: nothing — the sibling in `test_maestro_e2e.py` is the one where it is not free.
SCOPE = [TARGET_HOST]

#: Reuse a specific device instead of creating one. Useful for debugging a failure by hand, but
#: not the default: capture installs an Interception Authority into the device's trust store, and
#: doing that to a simulator the developer actually works in is not this test's business.
REUSE_UDID = os.environ.get("HTTPS_PROXY_MCP_E2E_UDID")

DEVICE_TYPE = os.environ.get("HTTPS_PROXY_MCP_E2E_DEVICE", "iPhone 17 Pro")


@pytest.fixture
def simulator():
    """A throwaway booted simulator, deleted afterwards.

    Created rather than borrowed. Capturing installs an authority into the device's trust store,
    and while that is per-device and disposable — which is exactly why SPEC.md makes it implicit —
    "disposable" is a property of a simulator the test made, not of one the developer relies on.

    `simctl create` writes a runtime notice to **stderr**, so the streams are kept apart. Folding
    them together puts that notice into the UDID and every later command fails with "Invalid
    device", which is a confusing five minutes if you have not seen it before.
    """
    if REUSE_UDID:
        yield REUSE_UDID
        return

    udid = subprocess.run(
        ["xcrun", "simctl", "create", "hpm-e2e-throwaway", DEVICE_TYPE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
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


async def test_a_real_simulator_app_s_https_traffic_is_captured(tmp_path, simulator):
    """Boot-to-body: route the machine, open a URL in the simulator, read the response back.

    Asserts on a decrypted HTTPS body, because that is the claim P3 makes. Plain metadata would
    be satisfied by a CONNECT tunnel the proxy could not read into, which is precisely the
    failure an untrusted authority produces.
    """
    before = read_real_state()
    state = CaptureState(tmp_path)
    server = build_server(state=state)

    try:
        started = await call(
            server, "start_capture", target="ios-simulator", port=0, udid=simulator, scope=SCOPE
        )
        assert started["trust"]["trusted"] is True, started["trust"]["detail"]

        # A real app on the device, making a real HTTPS request.
        subprocess.run(
            ["xcrun", "simctl", "openurl", simulator, TARGET_URL], check=True, timeout=60
        )

        waited = await call(
            server,
            "wait_for_exchanges",
            filter=f"~d {TARGET_HOST}",
            timeout_ms=60_000,
        )
        assert waited["timed_out"] is False, (
            "no traffic arrived from the simulator; diagnose_capture says:"
            f" {await call(server, 'diagnose_capture')}"
        )

        diagnosis = await call(server, "diagnose_capture")
        assert diagnosis["status"] == "ok", diagnosis

        exchange_id = waited["exchanges"][0]["id"]
        detail = await call(server, "get_exchange", exchange_id=exchange_id)
        assert detail["scheme"] == "https"
        # A status code and a readable body are only available if the TLS was actually
        # intercepted. A CONNECT tunnel the proxy could not read into — what an untrusted
        # authority produces — would have neither.
        assert detail["status"] == 200, detail
        assert detail["response_body"]["preview"], detail["response_body"]
    finally:
        if state.recording:
            await call(server, "stop_capture")

    assert read_real_state() == before, "the machine's proxy configuration was not restored"


async def test_stopping_restores_the_machine_exactly(tmp_path, simulator):
    """Round-trip fidelity against the machine's real prior state, whatever it happens to be."""
    before = read_real_state()
    state = CaptureState(tmp_path)
    server = build_server(state=state)

    await call(server, "start_capture", target="ios-simulator", port=0, udid=simulator, scope=SCOPE)
    try:
        during = read_real_state()
        assert during.web.enabled is True
        assert during.web.server == "127.0.0.1"
    finally:
        await call(server, "stop_capture")

    assert read_real_state() == before
