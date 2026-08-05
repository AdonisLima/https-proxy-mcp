"""Shared fixtures: a local HTTP server and a way to pick a port nobody is using."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from fixture_server import serve


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
