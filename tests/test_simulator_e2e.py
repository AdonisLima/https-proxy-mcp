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

#: The device the developer named for this work.
UDID = os.environ.get("HTTPS_PROXY_MCP_E2E_UDID", "F48AAE9F-4D3F-4817-9050-7B4B0024CF07")

#: Fetched over HTTPS from inside the simulator. Deliberately not a denylisted host, and
#: deliberately somewhere stable that exists to be fetched.
TARGET_URL = "https://example.com/"
TARGET_HOST = "example.com"


def read_real_state() -> object:
    """The machine's actual proxy configuration, through the real binaries."""
    return SystemProxy("/nonexistent-sentinel").read_state()


async def test_a_real_simulator_app_s_https_traffic_is_captured(tmp_path):
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
            server, "start_capture", target="ios-simulator", port=0, udid=UDID
        )
        assert started["trust"]["trusted"] is True, started["trust"]["detail"]

        # A real app on the device, making a real HTTPS request.
        subprocess.run(["xcrun", "simctl", "openurl", UDID, TARGET_URL], check=True, timeout=60)

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
        # The body proves the authority was trusted and the TLS was actually intercepted.
        assert detail["response"]["body"]["preview"]
    finally:
        if state.recording:
            await call(server, "stop_capture")

    assert read_real_state() == before, "the machine's proxy configuration was not restored"


async def test_stopping_restores_the_machine_exactly(tmp_path):
    """Round-trip fidelity against the machine's real prior state, whatever it happens to be."""
    before = read_real_state()
    state = CaptureState(tmp_path)
    server = build_server(state=state)

    await call(server, "start_capture", target="ios-simulator", port=0, udid=UDID)
    try:
        during = read_real_state()
        assert during.web.enabled is True
        assert during.web.server == "127.0.0.1"
    finally:
        await call(server, "stop_capture")

    assert read_real_state() == before
