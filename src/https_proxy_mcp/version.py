"""This project's version, resolved from its installed metadata.

``pyproject.toml`` is the only place a version number is written. Reading it back through
``importlib.metadata`` rather than repeating it in the source keeps that true — a literal here
would be a second source of truth for one fact, and the drift shows up as a released artifact
reporting a version nobody shipped.

It matters more than a normal accessor because the value ends up in the MCP handshake's
``serverInfo.version``, which is how a client tells one build of this server from another.
``FastMCP.__init__`` takes no ``version``, and the low-level server it wraps falls back to
``importlib.metadata.version("mcp")`` — so without this the server reports the *SDK's* version,
a number that moves when the dependency does and says nothing about us.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

#: The distribution name in ``pyproject.toml``, which is what metadata is keyed on.
DISTRIBUTION = "https-proxy-mcp"

#: Reported when there is no metadata to read: a source tree that was never installed. Honest
#: rather than plausible — a client seeing this knows it is not talking to a release, whereas a
#: hardcoded fallback would claim to be one.
UNKNOWN_VERSION = "0+unknown"


def resolve_version() -> str:
    """This distribution's version, or :data:`UNKNOWN_VERSION` if it is not installed."""
    try:
        return distribution_version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
