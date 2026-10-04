"""Shared fixtures: a local HTTP server and a way to pick a port nobody is using."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from fixture_server import serve

#: The per-test cap for the machine-gated tests, applied via pytest-timeout's marker below.
#: Thirty minutes, against the suite-wide 120s: these tests boot a real simulator, install
#: Maestro's driver onto it, and wait out a flow timeout that alone is 600s — a cap sized for
#: the rest of the suite would kill them for being what they are. The marker says *which* tests;
#: what says *whether they run at all* is still the HTTPS_PROXY_MCP_E2E environment variable on
#: each module's skipif, and nothing here touches it.
MACHINE_TEST_TIMEOUT_SECONDS = 1800


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Scope the larger cap to the ``machine`` marker.

    pytest-timeout gives a marker priority over the ini ``timeout``, so marking the item is the
    whole mechanism. Done at collection rather than in each module's ``pytestmark`` so a fourth
    machine-gated module cannot forget it, and so this one place is the seam the gate test
    asserts against. The marker remains documentation for everything else — the env-var gate is
    what keeps these tests from redirecting the machine's networking.
    """
    for item in items:
        if item.get_closest_marker("machine") is not None:
            item.add_marker(pytest.mark.timeout(MACHINE_TEST_TIMEOUT_SECONDS))


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[int]:
    """A local HTTP server for the whole test session. Yields its port."""
    with serve() as port:
        yield port


@pytest.fixture
def free_port() -> int:
    """A port that was free a moment ago.

    Inherently racy, so prefer asking the proxy to bind port 0 and reporting back what it
    got. This exists for the cases that must know the port before anything starts.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
