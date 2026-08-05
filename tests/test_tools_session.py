"""Tests for the Session lifecycle tools, against a real in-process proxy.

Stubbing the proxy here would defeat the purpose: what these check is that start_capture
actually brings a listener up, that get_status tells the truth about it, and that
stop_capture puts the port back. Marked `integration` because they bind real sockets.
"""

from __future__ import annotations

import socket
from datetime import datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture.session import SESSIONS_DIRNAME, Session
from https_proxy_mcp.server import build_server
from https_proxy_mcp.server.state import CaptureState
from tool_calls import call

pytestmark = pytest.mark.integration


def accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@pytest.fixture
async def state(tmp_path):
    state = CaptureState(tmp_path / "root")
    yield state
    if state.recording:
        await state.stop()


@pytest.fixture
def server(state):
    return build_server(state=state)


# --- starting -----------------------------------------------------------------


async def test_starting_reports_the_session(server):
    result = await call(server, "start_capture", port=0)

    assert result["recording"] is True
    assert result["target"] == "curl"
    assert result["session"]
    assert result["port"] != 0


async def test_starting_brings_a_listener_up(server):
    result = await call(server, "start_capture", port=0)

    assert accepts(result["port"])


async def test_starting_reports_trust(server):
    result = await call(server, "start_capture", port=0)

    assert result["trust"]["trusted"] is True
    assert "curl" in result["trust"]["detail"]


async def test_starting_creates_the_session_directory(server, tmp_path):
    result = await call(server, "start_capture", port=0)

    assert (tmp_path / "root" / SESSIONS_DIRNAME / result["session"]).is_dir()


async def test_starting_twice_is_refused(server):
    await call(server, "start_capture", port=0)

    with pytest.raises(ToolError, match="already recording"):
        await server.call_tool("start_capture", {"port": 0})


async def test_an_unknown_target_names_the_ones_that_exist(server):
    with pytest.raises(ToolError, match="curl"):
        await server.call_tool("start_capture", {"target": "nintendo-ds", "port": 0})


async def test_an_unknown_target_leaves_nothing_recording(server):
    with pytest.raises(ToolError):
        await server.call_tool("start_capture", {"target": "nintendo-ds", "port": 0})

    assert (await call(server, "get_status"))["recording"] is False


async def test_a_scope_is_recorded(server):
    result = await call(server, "start_capture", port=0, scope=["api.example.com"])

    assert result["scope"] == ["api.example.com"]


async def test_older_sessions_are_pruned_on_start(server, tmp_path):
    """The Session directory holds unredacted bodies, so retention is a privacy control."""
    for second in range(1, 13):
        Session.create(
            tmp_path / "root",
            target="curl",
            port=1,
            now=datetime(2026, 1, 1, 0, 0, second),
        )

    await call(server, "start_capture", port=0)

    kept = list((tmp_path / "root" / SESSIONS_DIRNAME).iterdir())
    assert len(kept) == 10


# --- status -------------------------------------------------------------------


async def test_status_before_anything_started(server):
    result = await call(server, "get_status")

    assert result["recording"] is False


async def test_status_while_recording_reports_the_details(server):
    started = await call(server, "start_capture", port=0)

    result = await call(server, "get_status")

    assert result["recording"] is True
    assert result["session"] == started["session"]
    assert result["port"] == started["port"]
    assert result["exchanges"] == 0
    assert result["evicted"] == 0


async def test_status_names_the_session_directory(server):
    await call(server, "start_capture", port=0)

    result = await call(server, "get_status")

    assert result["session_dir"].endswith(result["session"])


# --- stopping -----------------------------------------------------------------


async def test_stopping_reports_the_tally_and_where_it_went(server):
    started = await call(server, "start_capture", port=0)

    result = await call(server, "stop_capture")

    assert result["recording"] is False
    assert result["exchanges"] == 0
    assert result["session"] == started["session"]
    assert started["session"] in result["session_dir"]


async def test_stopping_releases_the_port(server):
    started = await call(server, "start_capture", port=0)

    await call(server, "stop_capture")

    assert not accepts(started["port"])


async def test_stopping_when_nothing_is_recording_is_refused(server):
    with pytest.raises(ToolError, match="not recording"):
        await server.call_tool("stop_capture", {})


async def test_a_session_can_be_started_again_after_stopping(server):
    first = await call(server, "start_capture", port=0)
    await call(server, "stop_capture")

    second = await call(server, "start_capture", port=0)

    assert second["session"] != first["session"]


async def test_the_exchanges_log_survives_stopping(server, tmp_path):
    result = await call(server, "start_capture", port=0)
    await call(server, "stop_capture")

    session_dir = tmp_path / "root" / SESSIONS_DIRNAME / result["session"]
    assert (session_dir / "exchanges.ndjson").exists()
