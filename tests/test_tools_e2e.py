"""P2 acceptance: a real MCP client captures curl traffic, pages it, slices a body, and waits.

The criterion from PLAN.md P2, checked as written. Everything here is real: a server
subprocess speaking MCP over stdio, mitmproxy running inside it, `curl` as the client under
test, and a local fixture server answering.

It exercises the arrangement ADR-0001 chose — proxy and server in one process — over a real
transport, which nothing else here does.

It does *not* guard stdout. That was worth checking rather than assuming, so it was: a
deliberate flushed ``print`` in the proxy startup path leaves every test below passing,
because the MCP client parses stdout line by line and skips what it cannot parse. Keeping the
proxy silent still matters — an interleaved write inside a JSON-RPC line would corrupt it —
but that is a discipline, not something these tests enforce.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fixture_server import BIG_RESPONSE, SECRET_VALUE
from https_proxy_mcp.capture.session import ROOT_ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl"),
]

TOKEN = "Bearer e2e-tool-token"


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """One Session root for the whole module.

    Shared so mitmproxy generates its Interception Authority once instead of per test — that
    keygen is most of the cost of starting a capture.
    """
    return tmp_path_factory.mktemp("mcp-root")


async def call(session: ClientSession, name: str, **arguments: Any) -> Any:
    """Call a tool over the wire and hand back its structured result."""
    result = await session.call_tool(name, arguments)

    assert not result.isError, f"{name} failed: {result.content}"
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


@asynccontextmanager
async def capturing(root: Path, fixture_port: int) -> AsyncIterator[tuple[ClientSession, int, str]]:
    """A real MCP client, connected and already capturing.

    An async context manager rather than a pytest fixture, deliberately: pytest-asyncio may
    run an async-generator fixture's setup and teardown in different tasks, and the anyio
    cancel scopes inside ``stdio_client`` refuse to be exited from a task other than the one
    that entered them. Keeping the whole lifetime inside the test body keeps it in one task.

    Yields the client, the proxy port, and the fixture server's base URL.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from https_proxy_mcp import main; main()"],
        # Sessions hold unredacted bodies; a test must never write them under a real home.
        env={"PATH": "/usr/bin:/bin", ROOT_ENV: str(root)},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        started = await call(session, "start_capture", target="curl", port=0)
        try:
            yield session, started["port"], f"http://127.0.0.1:{fixture_port}"
        finally:
            await call(session, "stop_capture")


async def curl(proxy_port: int, url: str, *extra: str) -> None:
    """Drive one request through the proxy, failing with curl's own message."""
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        # An empty exclusion list, so localhost does not bypass the proxy.
        "--noproxy",
        "",
        "-x",
        f"http://127.0.0.1:{proxy_port}",
        url,
        *extra,
    ]
    result = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, f"curl failed: {result.stderr}"


async def only_exchange(client: ClientSession) -> dict[str, Any]:
    """The single Exchange captured so far, in detail."""
    listed = await call(client, "list_exchanges")
    return await call(client, "get_exchange", exchange_id=listed["exchanges"][0]["id"])


# --- the acceptance criterion -------------------------------------------------


async def test_capture_page_slice_and_wait(root, fixture_server):
    """Capture curl traffic, page through it, fetch a body slice, then wait for a new request.

    PLAN.md's P2 criterion, in the order it is written there.
    """
    async with capturing(root, fixture_server) as (client, port, base):
        for index in range(5):
            await curl(port, f"{base}/item/{index}")

        # Page through what was captured.
        first = await call(client, "list_exchanges", limit=2)
        assert first["returned"] == 2
        assert first["next_cursor"] is not None

        second = await call(client, "list_exchanges", limit=2, cursor=first["next_cursor"])
        assert [row["id"] for row in second["exchanges"]] == [3, 4]

        # Fetch one body slice.
        await curl(port, f"{base}/big")
        listed = await call(client, "list_exchanges", limit=50)
        big = next(row for row in listed["exchanges"] if row["path"] == "/big")

        sliced = await call(
            client,
            "get_exchange_body",
            exchange_id=big["id"],
            side="response",
            offset=0,
            length=64,
        )
        assert sliced["data"] == BIG_RESPONSE[:64].decode()
        assert sliced["total"] == len(BIG_RESPONSE)
        assert sliced["has_more"] is True

        # And return on a request fired *after* the wait began.
        async def fire_later() -> None:
            await asyncio.sleep(0.2)
            await curl(port, f"{base}/afterwards")

        firing = asyncio.create_task(fire_later())
        try:
            waited = await call(
                client, "wait_for_exchanges", filter="~u /afterwards", timeout_ms=20_000
            )
        finally:
            await firing

        assert waited["timed_out"] is False
        assert [row["path"] for row in waited["exchanges"]] == ["/afterwards"]


# --- the pieces, so a failure above says which part broke ---------------------


async def test_status_reflects_real_captured_traffic(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me")
        await call(client, "wait_for_exchanges", filter="~u /me", timeout_ms=100)

        status = await call(client, "get_status")

        assert status["recording"] is True
        assert status["exchanges"] >= 1
        assert status["target"] == "curl"


async def test_an_exchange_captured_over_the_wire_has_real_detail(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me")

        detail = await only_exchange(client)

        assert detail["method"] == "GET"
        assert detail["status"] == 200
        assert detail["response_body"]["preview"] == '{"id":1,"name":"fixture"}'


async def test_a_body_preview_is_truncated_over_the_wire(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/big")

        preview = (await only_exchange(client))["response_body"]

        assert preview["truncated"] is True
        assert preview["shown_bytes"] == 2048
        assert "get_exchange_body" in preview["preview"]


# --- redaction, end to end ----------------------------------------------------


async def test_a_secret_in_a_real_response_does_not_reach_the_client(root, fixture_server):
    """The redaction chain through a real client, rather than a unit calling a function."""
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/secret")

        detail = json.dumps(await only_exchange(client))

        assert SECRET_VALUE not in detail
        assert "<redacted:sha256:" in detail


async def test_a_request_header_secret_does_not_reach_the_client(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me", "-H", f"Authorization: {TOKEN}")

        assert TOKEN not in json.dumps(await only_exchange(client))


async def test_a_secret_can_be_revealed_when_it_is_the_question(root, fixture_server):
    """ "Is my app sending the right token?" needs the real value, on explicit request."""
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me", "-H", f"Authorization: {TOKEN}")
        listed = await call(client, "list_exchanges")

        detail = await call(
            client,
            "get_exchange",
            exchange_id=listed["exchanges"][0]["id"],
            include_secrets=True,
        )

        assert TOKEN in json.dumps(detail)


async def test_the_session_directory_keeps_the_faithful_body(root, fixture_server):
    """Redacted at the boundary, faithful on disk. Both halves, asserted together."""
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/secret")
        status = await call(client, "get_status")

        bodies = (root / "sessions" / status["session"] / "bodies").glob("*.resp")

        assert any(SECRET_VALUE in path.read_text() for path in bodies)


# --- search, tail, export -----------------------------------------------------


async def test_searching_real_traffic_with_a_filter_expression(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me")
        await curl(port, f"{base}/submit", "-X", "POST", "--data", "a=1")

        found = await call(client, "search_exchanges", filter="~m POST")

        assert [row["path"] for row in found["exchanges"]] == ["/submit"]


async def test_tailing_real_traffic_returns_only_what_is_new(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/first")
        first = await call(client, "tail_exchanges")

        await curl(port, f"{base}/second")
        second = await call(client, "tail_exchanges", cursor=first["next_cursor"])

        assert [row["path"] for row in second["exchanges"]] == ["/second"]


async def test_exporting_curl_for_real_captured_traffic(root, fixture_server):
    async with capturing(root, fixture_server) as (client, port, base):
        await curl(port, f"{base}/me")
        listed = await call(client, "list_exchanges")

        exported = await call(client, "export_curl", exchange_id=listed["exchanges"][0]["id"])

        assert exported["command"].startswith("curl -X GET")
        assert "/me" in exported["command"]
