"""The Human View surfaced: ``start_capture``'s ``web_ui``/``web_port``, and what reports them.

Ticket 02 of the Human View feature (``.scratch/human-view/spec.md``). That the view serves
and tears down is test_runtime.py's; what is asserted here is the path from the tool surface
to it — the parameters reach the runtime unchanged, the URL is carried on every branch that
should carry it, and a failed view start leaves nothing half-started.

The fast half drives ``CaptureState`` through the non-binding runtime stub, like the state
section of test_passthrough.py. The one end-to-end check binds real sockets and is marked
``integration``; the stdio probe that shares this ticket's acceptance criteria lives in
test_handshake.py, beside the protocol tests.
"""

from __future__ import annotations

import errno
import urllib.parse

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from stub_runtime import StubRuntime
from tool_calls import call


class OccupiedWebPortRuntime(StubRuntime):
    """A runtime whose view cannot bind its port, as the real one reports an occupied web_port."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stopped = False

    async def start(self) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    async def stop(self) -> None:
        self.stopped = True
        await super().stop()


@pytest.fixture
def runtimes() -> list[StubRuntime]:
    return []


@pytest.fixture
async def state(tmp_path, runtimes):
    """A state whose runtimes are handed over for inspection rather than bound to a port."""

    def factory(store, **options):
        runtime = StubRuntime(store, **options)
        runtimes.append(runtime)
        return runtime

    state = CaptureState(tmp_path / "root", runtime_factory=factory)
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
def server(state):
    return build_server(state=state)


# --- the plumbing ---------------------------------------------------------------


async def test_the_view_parameters_reach_the_runtime_unchanged(server, runtimes):
    await call(server, "start_capture", port=0, web_ui=True, web_port=8443)

    assert runtimes[-1].web_ui is True
    assert runtimes[-1].web_port == 8443


async def test_the_view_defaults_to_off(server, runtimes):
    """No flag, no view: an existing caller sees no new parameter, no port and no URL."""
    result = await call(server, "start_capture", port=0)

    assert runtimes[-1].web_ui is False
    assert runtimes[-1].web_port == 0
    assert result["web_url"] is None


# --- what the start response and get_status report -------------------------------


async def test_the_start_result_carries_the_view_url(server, runtimes):
    """The developer opens what this returns; nothing else in the response names where to look."""
    result = await call(server, "start_capture", port=0, web_ui=True)

    assert result["web_url"] is not None
    assert result["web_url"] == runtimes[-1].web_url


async def test_status_carries_the_view_url_while_recording(server):
    """So the URL is recoverable after the start response is long gone."""
    started = await call(server, "start_capture", port=0, web_ui=True)

    status = await call(server, "get_status")

    assert status["web_url"] == started["web_url"]


async def test_status_carries_the_view_field_with_the_view_off(server):
    """Null, not absent — the same-keys rule applies to the boring case too."""
    await call(server, "start_capture", port=0)

    assert (await call(server, "get_status"))["web_url"] is None


async def test_status_carries_the_view_field_when_nothing_is_recording(server):
    """The branch an agent reads when something is wrong is exactly where a missing key bites."""
    assert (await call(server, "get_status"))["web_url"] is None


# --- a failed view start ----------------------------------------------------------


async def test_an_occupied_web_port_fails_start_capture_cleanly(tmp_path):
    """The existing half-started cleanup covers the view too: no Session, and the runtime
    is stopped rather than left holding whatever it did manage to bind."""
    runtimes: list[OccupiedWebPortRuntime] = []

    def factory(store, **options):
        runtime = OccupiedWebPortRuntime(store, **options)
        runtimes.append(runtime)
        return runtime

    server = build_server(state=CaptureState(tmp_path / "root", runtime_factory=factory))

    with pytest.raises(ToolError, match="Address already in use"):
        await server.call_tool(
            "start_capture", {"target": "curl", "port": 0, "web_ui": True, "web_port": 8443}
        )

    assert runtimes[-1].stopped is True
    assert (await call(server, "get_status"))["recording"] is False


# --- end to end, over a real runtime -----------------------------------------------

# The runtime's own tests prove the view serves and tears down; this one proves the wiring
# between the tool surface and it, which is the part those tests stub away.


@pytest.fixture
async def real_state(tmp_path):
    state = CaptureState(tmp_path / "root")
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
def real_server(real_state):
    return build_server(state=real_state)


@pytest.mark.integration
async def test_the_tool_surface_reports_a_real_view_url(real_server):
    """The start response and get_status agree on the runtime's token-gated loopback URL."""
    started = await call(real_server, "start_capture", port=0, web_ui=True)

    web_url = started["web_url"]
    assert web_url is not None
    parsed = urllib.parse.urlparse(web_url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    assert parsed.query.startswith("token=")

    assert (await call(real_server, "get_status"))["web_url"] == web_url
